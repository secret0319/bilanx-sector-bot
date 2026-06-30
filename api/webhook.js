// 텔레그램 webhook 핸들러
// 사용자가 섹터 버튼을 누르면 해당 섹터의 뉴스 리스트(원문 링크 포함)로 메시지를 편집한다.

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(200).json({ ok: true });
  }

  const update = req.body;
  const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;

  try {
    if (update.callback_query) {
      const query = update.callback_query;
      const data = query.data;
      const chatId = query.message.chat.id;
      const messageId = query.message.message_id;

      const sectorData = await fetchSectorData();

      if (data === 'back') {
        await editToMainView(BOT_TOKEN, chatId, messageId, sectorData);
      } else if (data.startsWith('sector:')) {
        const sectorName = data.replace('sector:', '');
        await editToSectorNews(BOT_TOKEN, chatId, messageId, sectorName, sectorData);
      }

      await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/answerCallbackQuery`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ callback_query_id: query.id }),
      });
    }

    return res.status(200).json({ ok: true });
  } catch (err) {
    console.error('Webhook 에러:', err.message);
    return res.status(200).json({ ok: true });
  }
}

async function fetchSectorData() {
  const url = process.env.SECTOR_DATA_URL;
  if (!url) throw new Error('SECTOR_DATA_URL 환경변수가 설정되지 않았습니다');
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`섹터 데이터 fetch 실패: ${res.status}`);
  return res.json();
}

function buildMainKeyboard(payload) {
  const sectors = Object.entries(payload.sectors).sort(
    (a, b) => Math.abs(b[1].change_percent || 0) - Math.abs(a[1].change_percent || 0)
  );
  const buttons = [];
  let row = [];
  for (const [name, info] of sectors) {
    const pct = info.change_percent || 0;
    const arrow = pct >= 0 ? '▲' : '▼';
    row.push({
      text: `${name} ${arrow}${Math.abs(pct).toFixed(1)}%`,
      callback_data: `sector:${name}`,
    });
    if (row.length === 2) {
      buttons.push(row);
      row = [];
    }
  }
  if (row.length) buttons.push(row);
  return { inline_keyboard: buttons };
}

async function editToMainView(token, chatId, messageId, payload) {
  const today = new Date().toLocaleDateString('ko-KR');
  const text = `📊 <b>BILANX RESEARCH</b>\n오늘의 섹터별 트래픽 (${today})\n\n섹터를 누르면 관련 뉴스를 볼 수 있어요.`;
  await editMessage(token, chatId, messageId, text, buildMainKeyboard(payload));
}

// HTML 파싱 오류 방지를 위해 < > & 이스케이프
function escapeHtml(str) {
  return (str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function editToSectorNews(token, chatId, messageId, sectorName, payload) {
  const sector = payload.sectors[sectorName];
  if (!sector) return;

  const pct = sector.change_percent || 0;
  let text = `📊 <b>${escapeHtml(sectorName)}</b> (${pct >= 0 ? '+' : ''}${pct}%)\n\n`;

  if (!sector.news || sector.news.length === 0) {
    text += '오늘 수집된 뉴스가 없습니다.';
  } else {
    sector.news.forEach((n, i) => {
      const title = escapeHtml(n.title);
      const source = escapeHtml(n.source);
      // 링크가 있으면 제목 자체를 클릭 가능한 링크로 표시
      const titleLine = n.link
        ? `<a href="${n.link}">${title}</a>`
        : title;
      text += `${i + 1}. ${titleLine}\n   <i>${source} · ${n.time}</i>\n\n`;
    });
  }

  const keyboard = {
    inline_keyboard: [[{ text: '🔙 섹터 목록으로', callback_data: 'back' }]],
  };

  await editMessage(token, chatId, messageId, text, keyboard);
}

async function editMessage(token, chatId, messageId, text, replyMarkup) {
  const res = await fetch(`https://api.telegram.org/bot${token}/editMessageText`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      chat_id: chatId,
      message_id: messageId,
      text,
      parse_mode: 'HTML',
      reply_markup: replyMarkup,
      disable_web_page_preview: true,
    }),
  });
  const json = await res.json();
  if (!json.ok) {
    console.error('editMessageText 실패:', JSON.stringify(json));
  }
  return json;
}
