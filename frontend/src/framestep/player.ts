// WebCodecs（mediabunny 経由）でフレーム番号厳密なコマ送り再生を行う薄いラッパー。
//
// <video>.currentTime は秒しか受け取らず、丸めと実際の表示フレームの対応が
// 保証されない（issue #19）。ここでは逆に、コンテナから読んだ全フレームの
// 提示時刻（timestamp）をロード時に一度だけ表にしておき、
// 「n 番目のフレーム」を常にその表引きで確定させる。
//
// mediabunny は Input + VideoSampleSink を持ち、getSample(timestamp) で
// 任意時刻のフレームを取れる。GOP 付きのソースでも、直前のキーフレームから
// 目的の timestamp まで内部でデコードして返す（呼び出し側は意識しない）。

// automosaic の動画はすべて mp4 で出しているので、フォーマットは MP4 だけに絞る。
// ALL_FORMATS だと未使用の MKV/WebM/HLS 等のパーサまでバンドルに含まれ、
// 実測でバンドルサイズが約2倍になった。
import { Input, MP4, UrlSource, VideoSampleSink } from "mediabunny";
import type { VideoSample } from "mediabunny";

export interface FrameStepPlayerInfo {
  frameCount: number;
  fps: number;
  width: number;
  height: number;
  durationSec: number;
}

export class FrameStepPlayer {
  private input: Input | null = null;
  private sink: VideoSampleSink | null = null;
  // フレーム番号 -> 提示時刻(秒)。索引段階で1回だけ全走査して作る。
  private timestamps: number[] = [];
  info: FrameStepPlayerInfo | null = null;

  constructor(private readonly url: string) {}

  async load(): Promise<FrameStepPlayerInfo> {
    this.input = new Input({ source: new UrlSource(this.url), formats: [MP4] });
    const track = await this.input.getPrimaryVideoTrack();
    if (!track) throw new Error("映像トラックが見つかりません");
    this.sink = new VideoSampleSink(track);

    // 全フレームの提示時刻を1回だけ走査して表にする。
    // ここが「n 番目のフレーム」を時間ベースの近似ではなく確定させる部分。
    const ts: number[] = [];
    for await (const sample of this.sink.samples()) {
      ts.push(sample.timestamp);
      sample.close();
    }
    if (ts.length === 0) throw new Error("フレームを1枚も読めません");
    this.timestamps = ts;

    const width = await track.getDisplayWidth();
    const height = await track.getDisplayHeight();
    const first = ts[0] as number;
    const last = ts[ts.length - 1] as number;
    const secondLast = ts.length > 1 ? (ts[ts.length - 2] as number) : first;
    const duration = last + (ts.length > 1 ? last - secondLast : 0);
    const fps = ts.length > 1 ? (ts.length - 1) / (last - first) : 0;

    this.info = { frameCount: ts.length, fps, width, height, durationSec: duration };
    return this.info;
  }

  get frameCount(): number {
    return this.timestamps.length;
  }

  // n 番目のフレームを取得する。表引きした厳密な timestamp で getSample するので、
  // 同じ n を何度呼んでも同じフレームが返る。
  async getFrame(n: number): Promise<VideoSample> {
    if (!this.sink) throw new Error("load() が先");
    if (n < 0 || n >= this.timestamps.length) {
      throw new Error(`フレーム番号が範囲外です: ${n}（0〜${this.timestamps.length - 1}）`);
    }
    const sample = await this.sink.getSample(this.timestamps[n] as number);
    if (!sample) throw new Error(`フレーム ${n} をデコードできません`);
    return sample;
  }

  // フレーム n の提示時刻が表とどれだけ厳密に一致しているか（診断用）。
  timestampOf(n: number): number {
    return this.timestamps[n] as number;
  }

  dispose(): void {
    this.input?.dispose();
    this.input = null;
    this.sink = null;
  }
}

export function webCodecsSupported(): boolean {
  return typeof (globalThis as { VideoDecoder?: unknown }).VideoDecoder === "function";
}
