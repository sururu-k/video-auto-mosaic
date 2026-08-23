"""MMDetection の Cascade Mask R-CNN を MMDeploy で ONNX に出す。

このスクリプトは専用の仮想環境 `.venv-mmdet` で動かす。メインの `.venv` には
mmdet / mmcv / torch を入れない。

    .venv-mmdet\\Scripts\\python.exe tools\\export_mmdet_onnx.py \
        --model cascade-mask-rcnn_r101_fpn_20e_coco \
        --checkpoint weights/cascade_mask_rcnn_r101_fpn_20e_coco.pth \
        --out weights/cascade_mask_rcnn_r101_fpn.onnx

MMDeploy の deploy config は _base_ の相互参照が面倒なので、ここで
1ファイルにまとめて書き出してから使う。
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# instance-seg_onnxruntime_dynamic.py を _base_ 展開したもの
DEPLOY_CFG = '''
onnx_config = dict(
    type='onnx',
    export_params=True,
    keep_initializers_as_inputs=False,
    opset_version=11,
    save_file='end2end.onnx',
    input_names=['input'],
    output_names=['dets', 'labels', 'masks'],
    input_shape=None,
    optimize=True,
    dynamic_axes={
        'input': {0: 'batch', 2: 'height', 3: 'width'},
        'dets': {0: 'batch', 1: 'num_dets'},
        'labels': {0: 'batch', 1: 'num_dets'},
        'masks': {0: 'batch', 1: 'num_dets', 2: 'height', 3: 'width'},
    })
codebase_config = dict(
    type='mmdet',
    task='ObjectDetection',
    model_type='end2end',
    post_processing=dict(
        score_threshold=0.05,
        confidence_threshold=0.005,
        iou_threshold=0.5,
        max_output_boxes_per_class=200,
        pre_top_k=5000,
        keep_top_k=100,
        background_label_id=-1,
        export_postprocess_mask=False,
    ))
backend_config = dict(type='onnxruntime')
'''

# RTMDet-Ins は後処理が違う（マスクを動的畳み込みで作るので画像サイズのマスクを
# そのまま出す）。MMDeploy 公式も専用 deploy config を置いている。
# instance-seg_rtmdet-ins_onnxruntime_static-640x640.py 相当。
DEPLOY_CFG_RTMDET_INS = DEPLOY_CFG.replace(
    'export_postprocess_mask=False', 'export_postprocess_mask=True')


def find_model_cfg(name: str) -> str:
    """mmdet が pip で同梱している config を名前から引く。"""
    import mmdet

    mim = os.path.join(os.path.dirname(mmdet.__file__), '.mim', 'configs')
    if not name.endswith('.py'):
        name += '.py'
    if os.path.isabs(name) and os.path.exists(name):
        return name
    for root, _dirs, files in os.walk(mim):
        if os.path.basename(name) in files:
            return os.path.join(root, os.path.basename(name))
    raise SystemExit(f'config が見つからない: {name} (探索先 {mim})')


def patch_cuda_default_device() -> None:
    """CPU だけの環境で RTMDet-Ins を書き出せるようにする。

    MMDeploy の `rtmdet_ins_head` 書き換えが
      self.prior_generator.single_level_grid_priors(hw, level_idx=0)
    と呼んでおり、mmdet 側の既定が device='cuda' なので
    `Torch not compiled with CUDA enabled` で落ちる。
    CUDA が無い環境では cpu に読み替える。
    """
    import torch
    from mmdet.models.task_modules.prior_generators import point_generator

    if torch.cuda.is_available():
        return
    gen = point_generator.MlvlPointGenerator
    original = gen.single_level_grid_priors

    def patched(self, featmap_size, level_idx, dtype=torch.float32,
                device='cuda', with_stride=False):
        if isinstance(device, str) and device.startswith('cuda'):
            device = 'cpu'
        return original(self, featmap_size, level_idx, dtype=dtype,
                        device=device, with_stride=with_stride)

    gen.single_level_grid_priors = patched
    print('[patch] MlvlPointGenerator.single_level_grid_priors の '
          "既定 device を cpu に読み替えた")


def fix_where_dtype(path: str) -> int:
    """MMDeploy が出す型不整合の Where を直す。

    `_select_nms_index()` の labels 側パディングが
      batched_labels.where(cond, batched_labels.new_ones(1) * -1)
    となっており、X が int64（NMS のクラス添字）のまま Y だけ float32 の
    定数 -1.0 になる。ONNX の Where は X と Y が同型でないといけないので
    ORT がロード時に弾く（Type parameter (T) of Optype (Where) bound to
    different types）。PyTorch の型昇格に合わせて int 側を float に上げる。
    """
    import onnx
    from onnx import TensorProto, helper, shape_inference

    model = onnx.load(path)
    inferred = shape_inference.infer_shapes(model, strict_mode=False)
    types: dict[str, int] = {}
    for v in list(inferred.graph.value_info) + list(inferred.graph.output) + \
            list(inferred.graph.input):
        types[v.name] = v.type.tensor_type.elem_type
    for init in model.graph.initializer:
        types[init.name] = init.data_type
    for node in model.graph.node:
        if node.op_type == 'Constant' and node.attribute:
            types[node.output[0]] = node.attribute[0].t.data_type

    int_types = {TensorProto.INT8, TensorProto.INT16, TensorProto.INT32,
                 TensorProto.INT64, TensorProto.BOOL}
    fixed = 0
    new_nodes = []
    for node in model.graph.node:
        if node.op_type == 'Where':
            tx, ty = types.get(node.input[1]), types.get(node.input[2])
            if tx and ty and tx != ty:
                # int 側を float 側に合わせる
                if tx in int_types and ty not in int_types:
                    src, dst_type, idx = node.input[1], ty, 1
                elif ty in int_types and tx not in int_types:
                    src, dst_type, idx = node.input[2], tx, 2
                else:
                    new_nodes.append(node)
                    continue
                cast_out = f'{src}_castfix_{fixed}'
                new_nodes.append(helper.make_node(
                    'Cast', [src], [cast_out], to=dst_type,
                    name=f'CastFix_{fixed}'))
                node.input[idx] = cast_out
                fixed += 1
                print(f'[fix] {node.name}: input[{idx}] を '
                      f'{TensorProto.DataType.Name(dst_type)} にキャスト')
        new_nodes.append(node)

    if fixed:
        del model.graph.node[:]
        model.graph.node.extend(new_nodes)
        onnx.save(model, path)
    print(f'[fix] Where の型不整合 {fixed} 件を修正')
    return fixed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', default='cascade-mask-rcnn_r101_fpn_20e_coco',
                    help='mmdet の config 名（.py 省略可）')
    ap.add_argument('--checkpoint', required=True)
    ap.add_argument('--out', required=True, help='出力 onnx のパス')
    ap.add_argument('--img', default='data/bench3/clips/check/0501_f000000.png')
    ap.add_argument('--input-shape', default='', help='例 1333x800。空なら動的')
    ap.add_argument('--opset', type=int, default=11)
    ap.add_argument('--preset', default='maskrcnn',
                    choices=['maskrcnn', 'rtmdet-ins'])
    ap.add_argument('--keep-top-k', type=int, default=100,
                    help='NMS 後に残す検出数。マスク後処理の重さに直結する')
    ap.add_argument('--max-per-img', type=int, default=0,
                    help='model.test_cfg.max_per_img を上書きする。'
                         'RTMDet-Ins はこちらがマスク復号の本数を決める')
    args = ap.parse_args()

    model_cfg = find_model_cfg(args.model)
    print(f'[cfg] {model_cfg}')

    work = os.path.join(ROOT, 'weights', '_mmdeploy_work')
    os.makedirs(work, exist_ok=True)
    deploy_path = os.path.join(
        work, f'deploy_{args.preset}_k{args.keep_top_k}_ort.py')
    body = DEPLOY_CFG_RTMDET_INS if args.preset == 'rtmdet-ins' else DEPLOY_CFG
    if args.opset != 11:
        body = body.replace('opset_version=11', f'opset_version={args.opset}')
    if args.keep_top_k != 100:
        body = body.replace('keep_top_k=100', f'keep_top_k={args.keep_top_k}')
        body = body.replace('max_output_boxes_per_class=200',
                            f'max_output_boxes_per_class={args.keep_top_k}')
    if args.input_shape:
        w, h = (int(x) for x in args.input_shape.lower().split('x'))
        body = body.replace('input_shape=None', f'input_shape=[{w}, {h}]')
    with open(deploy_path, 'w', encoding='utf-8') as f:
        f.write(body)

    from mmdeploy.apis import torch2onnx  # noqa: E402

    patch_cuda_default_device()

    out_dir = os.path.dirname(os.path.abspath(args.out)) or '.'
    os.makedirs(out_dir, exist_ok=True)
    save_file = os.path.basename(args.out)

    model_cfg_arg = model_cfg
    if args.max_per_img:
        from mmengine.config import Config
        cfg = Config.fromfile(model_cfg)
        cfg.model.test_cfg.max_per_img = args.max_per_img
        model_cfg_arg = cfg
        print(f'[cfg] model.test_cfg.max_per_img = {args.max_per_img}')

    torch2onnx(
        os.path.abspath(args.img),
        out_dir,
        save_file,
        deploy_cfg=deploy_path,
        model_cfg=model_cfg_arg,
        model_checkpoint=os.path.abspath(args.checkpoint),
        device='cpu',
    )
    produced = os.path.join(out_dir, save_file)
    fix_where_dtype(produced)
    print(f'[done] {produced}  {os.path.getsize(produced) / 1e6:.1f} MB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
