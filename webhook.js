// 텔레그램 webhook 핸들러
// 사용자가 섹터 버튼을 누르면 해당 섹터의 뉴스 리스트로 메시지를 편집한다.

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(200).json({ ok: true });
  }

  const update = req.body;
  const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;

  console.log('=== Webhook 호출됨 ===');
  console.log('update 전체:', JSON.stringify(update));

  try {
    if (update.callback_query) {
      const query = update.callback_query;
      const data = query.data;
      const chatId = query.message.chat.id;
      const messageId = query.message.message_id;

      console.log(`콜백 데이터: ${data}, chatId: ${chatId}, messageId: ${messageId}`);

      const sectorData = await fetchSectorData();
      console.log('섹터 데이터 로드 성공, 키 개수:', Object.keys(sectorData.sectors || {}).length);

      if (data === 'back') {
        await editToMainView(BOT_TOKEN, chatId, messageId, sectorData);
      } else if (data.startsWith('sector:')) {
        const sectorName = data.replace('sector:', '');
        console.log('요청된 섹터명:', sectorName);
        console.log('존재하는 섹터 키들:', Object.keys(sectorData.sectors));
        await editToSectorNews(BOT_TOKEN, chatId, messageId, sectorName, sectorData);
      }

      const callbackRes = await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/answerCallbackQuery`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ callback_query_id: query.id }),
      });
      const callbackJson = await callbackRes.json();
      console.log('answerCallbackQuery 결과:', JSON.stringify(callbackJson));
    } else {
      console.log('callback_query 없음 - 일반 메시지이거나 다른 업데이트 타입');
    }

    return res.status(200).json({ ok: true });
  } catch (err) {
    console.error('=== Webhook 에러 발생 ===');
    console.error(err.message);
    console.error(err.stack);
    return res.status(200).json({ ok: true });
  }
}

async function fetchSectorData() {
  const url = process.env.SECTOR_DATA_URL;
  console.log('SECTOR_DATA_URL:', url);
  if (!url) {
    throw new Error('SECTOR_DATA_URL 환경변수가 설정되지 않았습니다');
  }
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) {
    throw new Error(`섹터 데이터 fetch 실패: ${res.status}`);
  }
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

async function editToSectorNews(token, chatId, messageId, sectorName, payload) {
  const sector = payload.sectors[sectorName];
  if (!sector) {
    console.error(`섹터 "${sectorName}"를 찾을 수 없음. 사용 가능한 키:`, Object.keys(payload.sectors));
    return;
  }

  const pct = sector.change_percent || 0;
  let text = `📊 <b>${sectorName}</b> (${pct >= 0 ? '+' : ''}${pct}%)\n\n`;
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
  const res = await fetch(`https://api.telegram.org/bot${token}/editMessageText`, {
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
  const json = await res.json();
  console.log('editMessageText 결과:', JSON.stringify(json));
  if (!json.ok) {
    console.error('editMessageText 실패! 텔레그램 응답:', JSON.stringify(json));
  }
  return json;
}
