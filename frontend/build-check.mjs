export function normalizeGeneratedText(text) {
  return text.replace(/\r\n/g, "\n");
}

export function generatedTextMatches(generated, committed) {
  return normalizeGeneratedText(generated) === normalizeGeneratedText(committed);
}

export function isCanonicalRemoteUrl(url) {
  const normalized = url
    .trim()
    .replace(/\\/g, "/")
    .replace(/\/+$/, "")
    .replace(/\.git$/i, "");

  if (/^(?:[^@/]+@)?github\.com:sururu-k\/video-auto-mosaic$/i.test(normalized)) {
    return true;
  }

  try {
    const parsed = new URL(normalized);
    return (
      parsed.hostname.toLowerCase() === "github.com" &&
      parsed.pathname.replace(/^\/+|\/+$/g, "").toLowerCase() === "sururu-k/video-auto-mosaic"
    );
  } catch {
    return false;
  }
}
