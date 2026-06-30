// 텔레그램 webhook 핸들러
// 사용자가 섹터 버튼을 누르면 해당 섹터의 뉴스 리스트로 메시지를 편집한다.

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(200).json({ ok: true });
  }

  const update = req.body;
  const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;

  try {
    if (update.callback_query) {
      const query = update.callback_query;
      const data = query.data; // "sector:정보기술" 또는 "back"
      const chatId = query.message.chat.id;
      const messageId = query.message.message_id;

      // 저장된 섹터 데이터 가져오기 (GitHub Raw에서 읽기)
      const sectorData = await fetchSectorData();

      if (data === 'back') {
        // 메인 화면으로 복귀
        await editToMainView(BOT_TOKEN, chatId, messageId, sectorData);
      } else if (data.startsWith('sector:')) {
        const sectorName = data.replace('sector:', '');
        await editToSectorNews(BOT_TOKEN, chatId, messageId, sectorName, sectorData);
      }

      // 콜백 응답 (로딩 표시 제거)
      await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/answerCallbackQuery`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ callback_query_id: query.id }),
      });
    }

    return res.status(200).json({ ok: true });
  } catch (err) {
    console.error(err);
    return res.status(200).json({ ok: true }); // 텔레그램은 항상 200 기대
  }
}

async function fetchSectorData() {
  // GitHub Actions가 매일 커밋하는 sector_data.json을 raw로 읽음
  const url = process.env.SECTOR_DATA_URL; // 예: https://raw.githubusercontent.com/.../sector_data.json
  const res = await fetch(url, { cache: 'no-store' });
  return res.json();
}

function buildMainKeyboard(payload) {
  const sectors = Object.entries(payload.sectors).sort(
    (a, b) => Math.abs(b[1].change_percent) - Math.abs(a[1].change_percent)
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

async function editToSectorNews(token, chatId, messageId, sectorName, payload) {
  const sector = payload.sectors[sectorName];
  if (!sector) return;

  let text = `📊 <b>${sectorName}</b> (${sector.change_percent >= 0 ? '+' : ''}${sector.change_percent}%)\n\n`;
  if (!sector.news || sector.news.length === 0) {
    text += '오늘 수집된 뉴스가 없습니다.';
  } else {
    sector.news.forEach((n, i) => {
      text += `${i + 1}. ${n.title}\n   <i>${n.source} · ${n.time}</i>\n\n`;
    });
  }

  const keyboard = {
    inline_keyboard: [[{ text: '🔙 섹터 목록으로', callback_data: 'back' }]],
  };

  await editMessage(token, chatId, messageId, text, keyboard);
}

async function editMessage(token, chatId, messageId, text, replyMarkup) {
  await fetch(`https://api.telegram.org/bot${token}/editMessageText`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      chat_id: chatId,
      message_id: messageId,
      text,
      parse_mode: 'HTML',
      reply_markup: replyMarkup,
    }),
  });
}
