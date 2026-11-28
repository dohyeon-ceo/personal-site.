// Vercel 서버리스 함수: GET /api/transcript?url=<유튜브 주소>&lang=<언어코드(선택)>  (화면: youtube-script/)
// 브라우저에서는 CORS 때문에 유튜브 자막을 직접 가져올 수 없어서, 서버에서 대신 가져온다.

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
const BASE_HEADERS = {
  'User-Agent': UA,
  'Accept-Language': 'ko,en;q=0.8',
  Cookie: 'CONSENT=YES+cb; SOCS=CAI',
};

function extractVideoId(input) {
  if (!input) return null;
  const s = String(input).trim();
  if (/^[\w-]{11}$/.test(s)) return s;
  let u;
  try {
    u = new URL(s.startsWith('http') ? s : 'https://' + s);
  } catch {
    return null;
  }
  const host = u.hostname.replace(/^www\.|^m\.|^music\./, '');
  if (host === 'youtu.be') return u.pathname.slice(1, 12) || null;
  if (host.endsWith('youtube.com') || host.endsWith('youtube-nocookie.com')) {
    const v = u.searchParams.get('v');
    if (v) return v.slice(0, 11);
    const m = u.pathname.match(/\/(?:shorts|embed|live|v)\/([\w-]{11})/);
    if (m) return m[1];
  }
  return null;
}

function decodeEntities(str) {
  const once = (s) =>
    s
      .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(+n))
      .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
      .replace(/&quot;/g, '"')
      .replace(/&apos;/g, "'")
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&nbsp;/g, ' ')
      .replace(/&amp;/g, '&');
  // 유튜브 자막은 종종 두 번 인코딩되어 있다 (&amp;#39; 등)
  return once(once(str));
}

function parseTimedText(xml) {
  const segments = [];
  // 형식 1: <text start="1.2" dur="3.4">...</text>
  const reText = /<text start="([\d.]+)"[^>]*>([\s\S]*?)<\/text>/g;
  let m;
  while ((m = reText.exec(xml))) {
    const text = decodeEntities(m[2].replace(/<[^>]+>/g, '')).replace(/\s+/g, ' ').trim();
    if (text) segments.push({ start: parseFloat(m[1]), text });
  }
  if (segments.length) return segments;
  // 형식 2 (srv3): <p t="1200" d="3400"><s>...</s></p>
  const reP = /<p t="(\d+)"[^>]*>([\s\S]*?)<\/p>/g;
  while ((m = reP.exec(xml))) {
    const text = decodeEntities(m[2].replace(/<[^>]+>/g, '')).replace(/\s+/g, ' ').trim();
    if (text) segments.push({ start: parseInt(m[1], 10) / 1000, text });
  }
  return segments;
}

class UserError extends Error {}

async function getPlayerResponse(videoId) {
  const pageRes = await fetch(`https://www.youtube.com/watch?v=${videoId}&hl=ko`, {
    headers: BASE_HEADERS,
  });
  const html = await pageRes.text();
  if (html.includes('class="g-recaptcha"')) {
    throw new UserError('유튜브가 서버 요청을 차단했습니다(봇 확인). 잠시 후 다시 시도해 주세요.');
  }
  const keyMatch = html.match(/"INNERTUBE_API_KEY":\s*"([^"]+)"/);
  if (!keyMatch) throw new Error('유튜브 페이지에서 API 키를 찾지 못했습니다.');

  const res = await fetch(`https://www.youtube.com/youtubei/v1/player?key=${keyMatch[1]}`, {
    method: 'POST',
    headers: { ...BASE_HEADERS, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      context: { client: { clientName: 'ANDROID', clientVersion: '20.10.38' } },
      videoId,
    }),
  });
  if (!res.ok) throw new Error(`유튜브 응답 오류 (${res.status})`);
  return res.json();
}

function trackName(t) {
  if (!t.name) return t.languageCode;
  return t.name.simpleText || (t.name.runs || []).map((r) => r.text).join('') || t.languageCode;
}

async function handler(req, res) {
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  // GitHub Pages(dohyeon-ceo.github.io)에 올린 화면에서도 이 서버를 부를 수 있게 허용
  const origin = req.headers.origin || '';
  if (origin === 'https://dohyeon-ceo.github.io' || /^http:\/\/localhost(:\d+)?$/.test(origin)) {
    res.setHeader('Access-Control-Allow-Origin', origin);
    res.setHeader('Vary', 'Origin');
  }
  const send = (status, body) => {
    res.statusCode = status;
    res.end(JSON.stringify(body));
  };

  try {
    const q = new URL(req.url, 'http://localhost').searchParams;
    const videoId = extractVideoId(q.get('url'));
    if (!videoId) return send(400, { error: '올바른 유튜브 주소가 아닙니다.' });

    const player = await getPlayerResponse(videoId);
    const status = player.playabilityStatus || {};
    if (status.status && status.status !== 'OK') {
      return send(400, { error: `영상을 불러올 수 없습니다: ${status.reason || status.status}` });
    }

    const rawTracks =
      player.captions?.playerCaptionsTracklistRenderer?.captionTracks || [];
    if (!rawTracks.length) {
      return send(404, { error: '이 영상에는 자막(스크립트)이 없습니다.' });
    }

    const tracks = rawTracks.map((t) => ({
      code: t.languageCode,
      name: trackName(t),
      auto: t.kind === 'asr',
      baseUrl: t.baseUrl,
    }));

    // 선택 우선순위: 요청한 언어 → 한국어(수동) → 수동 자막 → 첫 번째
    const want = q.get('lang');
    const pick =
      (want && tracks.find((t) => `${t.code}${t.auto ? ':auto' : ''}` === want)) ||
      (want && tracks.find((t) => t.code === want)) ||
      tracks.find((t) => t.code.startsWith('ko') && !t.auto) ||
      tracks.find((t) => !t.auto) ||
      tracks[0];

    const xmlRes = await fetch(pick.baseUrl.replace(/&fmt=[^&]*/, ''), { headers: BASE_HEADERS });
    const xml = await xmlRes.text();
    const segments = parseTimedText(xml);
    if (!segments.length) {
      return send(502, { error: '자막을 가져왔지만 내용이 비어 있습니다. 다시 시도해 주세요.' });
    }

    return send(200, {
      videoId,
      title: player.videoDetails?.title || '',
      author: player.videoDetails?.author || '',
      selected: `${pick.code}${pick.auto ? ':auto' : ''}`,
      tracks: tracks.map(({ code, name, auto }) => ({
        id: `${code}${auto ? ':auto' : ''}`,
        name: auto && !/자동|auto/i.test(name) ? `${name} (자동 생성)` : name,
      })),
      segments,
    });
  } catch (e) {
    return send(e instanceof UserError ? 429 : 500, {
      error: e.message || '알 수 없는 오류가 발생했습니다.',
    });
  }
}

module.exports = handler;
module.exports.extractVideoId = extractVideoId;
module.exports.parseTimedText = parseTimedText;
