
const $ = id => document.getElementById(id);

let archives = [];
let games = [];
let selected = null;
let parsed = null;
let cursor = 0;
let activeJob = null;
let analysisResult = null;

const setStatus = message => $('status').textContent = message;

const escapeHTML = value =>
  String(value ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[c]));

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(data.error || `خطأ من الخادم: ${response.status}`);
  }

  return data;
}

// ============================================================
// PLAYER SEARCH — ALL COMPLETED GAMES
// ============================================================

$('load').onclick = async () => {
  const username = $('username').value.trim();

  if (!/^[a-z0-9_-]{2,25}$/i.test(username)) {
    setStatus('اكتب اسم مستخدم صحيحاً (2–25 حرفاً/رقماً).');
    return;
  }

  $('load').disabled = true;
  $('games').innerHTML = '';
  $('month').disabled = true;

  setStatus('جارٍ جلب أرشيف اللاعب من Chess.com...');

  try {
    const data = await api(
      `/api/player/${encodeURIComponent(username)}/archives`
    );

    archives = data.archives || [];

    if (!archives.length) {
      throw new Error(
        'لم يتم العثور على أرشيف مباريات عام لهذا اللاعب.'
      );
    }

    const newestFirst = archives.slice().reverse();

    $('month').innerHTML = [
      '<option value="__all__">كل الأشهر — جميع المباريات المكتملة</option>',
      ...newestFirst.map(url => {
        const month = url.split('/').slice(-2).join('/');

        return `<option value="${escapeHTML(url)}">${escapeHTML(month)}</option>`;
      })
    ].join('');

    $('month').value = '__all__';
    $('month').disabled = false;

    await loadMonth('__all__');

  } catch (error) {
    setStatus(error.message);
  } finally {
    $('load').disabled = false;
  }
};

$('month').onchange = () => {
  loadMonth($('month').value);
};

// ============================================================
// LOAD MONTH OR ALL ARCHIVES
// ============================================================

function renderGames(list) {
  $('games').innerHTML = '';

  if (!list.length) {
    $('games').textContent =
      'لا توجد مباريات مكتملة في الفترة المحددة.';
    return;
  }

  for (const game of list) {
    const white = game.white?.username || 'White';
    const black = game.black?.username || 'Black';

    const date = game.end_time
      ? new Date(game.end_time * 1000).toLocaleDateString('ar')
      : '';

    const button = document.createElement('button');

    button.className = 'game secondary';

    button.innerHTML = `
      <span>
        <b>${escapeHTML(white)}</b> ×
        <b>${escapeHTML(black)}</b>
        <br>
        <small class="muted">
          ${escapeHTML(date)} ·
          ${escapeHTML(game.time_class || '')}
        </small>
      </span>
      <span class="tag">
        ${escapeHTML(game.white?.result || '—')} /
        ${escapeHTML(game.black?.result || '—')}
      </span>
    `;

    button.onclick = () => chooseGame(game, button);

    $('games').append(button);
  }
}

async function loadMonth(archive) {
  $('games').innerHTML = 'جارٍ تحميل المباريات...';

  try {
    if (archive !== '__all__') {
      const data = await api(
        `/api/games?archive=${encodeURIComponent(archive)}`
      );

      games = (data.games || []).slice().sort(
        (a, b) => (b.end_time || 0) - (a.end_time || 0)
      );

      renderGames(games);

      setStatus(
        `تم تحميل ${games.length} مباراة من أرشيف ${archive
          .split('/')
          .slice(-2)
          .join('/')}.`
      );

      return;
    }

    const collected = [];
    const failedMonths = [];

    const newestFirst = archives.slice().reverse();

    for (let i = 0; i < newestFirst.length; i++) {
      const url = newestFirst[i];

      const monthName = url
        .split('/')
        .slice(-2)
        .join('/');

      setStatus(
        `جارٍ جمع المباريات المكتملة: ${i + 1} من ${newestFirst.length} أشهر (${monthName})...`
      );

      try {
        const data = await api(
          `/api/games?archive=${encodeURIComponent(url)}`
        );

        collected.push(...(data.games || []));

      } catch (error) {
        failedMonths.push(monthName);
      }

      if (i < newestFirst.length - 1) {
        await new Promise(resolve => setTimeout(resolve, 180));
      }
    }

    const unique = new Map();

    for (const game of collected) {
      const id =
        game.uuid ||
        game.url ||
        `${game.end_time || 0}:${game.white?.username || ''}:${game.black?.username || ''}`;

      if (!unique.has(id)) {
        unique.set(id, game);
      }
    }

    games = Array.from(unique.values()).sort(
      (a, b) => (b.end_time || 0) - (a.end_time || 0)
    );

    renderGames(games);

    if (failedMonths.length) {
      setStatus(
        `تم تحميل ${games.length} مباراة، لكن تعذر تحميل ${failedMonths.length} شهر/أشهر: ${failedMonths.join('، ')}. اختر الشهر لإعادة المحاولة.`
      );
    } else {
      setStatus(
        `تم تحميل ${games.length} مباراة مكتملة من جميع الأشهر المتاحة (${archives.length} شهر).`
      );
    }

  } catch (error) {
    $('games').textContent = error.message;

    setStatus(
      'تعذر تحميل المباريات: ' + error.message
    );
  }
}

// ============================================================
// SELECT GAME
// ============================================================

async function chooseGame(game, element) {
  document.querySelectorAll('.game').forEach(button => {
    button.classList.remove('active');
  });

  element.classList.add('active');

  if (!game.pgn) {
    setStatus('هذه المباراة لا تحتوي على PGN متاح.');
    return;
  }

  selected = game;

  $('review').classList.remove('hidden');

  $('gameTitle').textContent =
    `${game.white?.username || 'White'} × ${game.black?.username || 'Black'}`;

  $('gameMeta').textContent =
    `${new Date((game.end_time || 0) * 1000).toLocaleString('ar')} · ${game.time_class || 'Chess'} · ${game.white?.rating || '?'} / ${game.black?.rating || '?'}`;

  $('moveList').innerHTML = '';

  $('bar').style.width = '0%';

  $('blunders').textContent = '0';
  $('mistakes').textContent = '0';
  $('accuracy').textContent = '—';

  $('progressText').textContent =
    'جارٍ تجهيز المباراة...';

  $('analyze').disabled = true;

  try {
    parsed = await api('/api/parse', {
      method: 'POST',

      headers: {
        'Content-Type': 'application/json'
      },

      body: JSON.stringify({
        pgn: game.pgn
      })
    });

    cursor = 0;
    analysisResult = null;

    $('progressText').textContent =
      `تم تحميل ${parsed.moves.length} نقلة. اضغط ابدأ التحليل.`;

    renderBoard();

    renderMoveReview();

    $('analyze').disabled = false;

  } catch (error) {
    $('progressText').textContent = error.message;
    parsed = null;
  }
}

// ============================================================
// NEO PIECES
// ============================================================

const PIECE_CDN =
  'https://images.chesscomfiles.com/chess-themes/pieces/neo/150/';

const pieceFallback = {
  k: '♚',
  q: '♛',
  r: '♜',
  b: '♝',
  n: '♞',
  p: '♟'
};

function makePiece(piece) {
  const key = piece.color + piece.type;

  const image = document.createElement('img');

  image.className = 'piece-img';
  image.alt = '';
  image.draggable = false;
  image.loading = 'eager';
  image.decoding = 'async';

  image.src = PIECE_CDN + key + '.png';

  image.onerror = () => {
    const span = document.createElement('span');

    span.textContent =
      pieceFallback[piece.type] || '●';

    span.style.cssText =
      'font-family:serif;font-size:clamp(27px,7vw,52px);line-height:1;color:' +
      (piece.color === 'w' ? '#fff' : '#17212b') +
      ';text-shadow:0 1px 2px #0008';

    image.replaceWith(span);
  };

  return image;
}

function piecesFromFen(fen) {
  const rows = fen.split(' ')[0].split('/');

  const board = [];

  for (const row of rows) {
    const squares = [];

    for (const char of row) {
      if (/\d/.test(char)) {
        for (let i = 0; i < Number(char); i++) {
          squares.push(null);
        }
      } else {
        squares.push({
          type: char.toLowerCase(),
          color: char === char.toUpperCase() ? 'w' : 'b'
        });
      }
    }

    board.push(squares);
  }

  return board;
}

// ============================================================
// ARABIC MOVE EVALUATION
// ============================================================

const MOVE_LABELS = {
  brilliant: 'نقلة عبقرية',
  great: 'نقلة عظيمة',
  best: 'أفضل نقلة',
  excellent: 'نقلة ممتازة',
  good: 'نقلة جيدة',
  book: 'نقلة افتتاحية',
  inaccuracy: 'نقلة غير دقيقة',
  mistake: 'خطأ',
  blunder: 'خطأ فادح',
  miss: 'تفويت فرصة'
};

function getMoveLabel(category) {
  return MOVE_LABELS[category] || 'تم تقييم النقلة';
}

// ============================================================
// CHESS BOARD
// ============================================================

function renderBoard() {
  if (!parsed?.positions?.length) return;

  const board = piecesFromFen(
    parsed.positions[cursor]
  );

  $('board').innerHTML = '';

  for (let rank = 0; rank < 8; rank++) {
    for (let file = 0; file < 8; file++) {
      const piece = board[rank][file];

      const square = document.createElement('div');

      square.className =
        'sq ' + ((rank + file) % 2 ? 'dark' : 'light');

      if (piece) {
        square.append(makePiece(piece));
      }

      $('board').append(square);
    }
  }

  const ply = parsed.moves.length;

  $('plyLabel').textContent =
    cursor === 0
      ? 'البداية'
      : `نقلة ${Math.ceil(cursor / 2)} · ${cursor % 2 ? 'الأبيض' : 'الأسود'}`;

  $('prev').disabled = cursor === 0;
  $('next').disabled = cursor === ply;
}

// ============================================================
// MOVE REVIEW — SHOW ONLY CURRENT MOVE
// ============================================================

function renderMoveReview() {
  const container = $('moveList');

  if (!container) return;

  if (!analysisResult) {
    container.innerHTML = `
      <div class="muted">
        ابدأ التحليل أولاً، ثم استخدم زر التالي لمراجعة كل نقلة.
      </div>
    `;
    return;
  }

  if (cursor === 0) {
    container.innerHTML = `
      <div class="muted">
        أنت في بداية المباراة. اضغط «التالي» لمراجعة أول نقلة وتقييمها.
      </div>
    `;
    return;
  }

  const move = analysisResult.moves?.find(
    item => Number(item.ply) === cursor
  );

  if (!move) {
    container.innerHTML = `
      <div class="muted">
        لا يوجد تقييم متاح لهذه الوضعية.
      </div>
    `;
    return;
  }

  const label = getMoveLabel(move.category);

  const score = Number(move.loss || 0).toFixed(2);

  const number = Math.floor((move.ply - 1) / 2) + 1;

  const turn = move.ply % 2 ? 'الأبيض' : 'الأسود';

  container.innerHTML = `
    <div class="move-review">
      <div class="muted">
        النقلة ${number} · ${turn}
      </div>

      <h3 class="review-result">
        ${escapeHTML(label)}
      </h3>

      <div class="review-details">
        <p>
          <strong>النقلة التي لعبتها:</strong>
          ${escapeHTML(move.san)}
        </p>

        <p>
          <strong>أفضل نقلة:</strong>
          ${escapeHTML(move.best || 'غير متوفر')}
        </p>

        <p>
          <strong>خسارة التقييم:</strong>
          ${escapeHTML(score)}
        </p>
      </div>
    </div>
  `;
}

function stepCursor(delta) {
  if (!parsed?.moves?.length) return;

  const next = Math.max(
    0,
    Math.min(parsed.moves.length, cursor + delta)
  );

  if (next === cursor) return;

  cursor = next;

  renderBoard();

  renderMoveReview();
}

$('prev').addEventListener('click', () => {
  stepCursor(-1);
});

$('next').addEventListener('click', () => {
  stepCursor(1);
});

// ============================================================
// SHOW ANALYSIS
// ============================================================

function showAnalysis(result) {
  analysisResult = result;

  renderBoard();

  renderMoveReview();

  $('blunders').textContent = result.blunders;
  $('mistakes').textContent = result.mistakes;
  $('accuracy').textContent = result.accuracy + '%';

  $('progressText').textContent =
    `اكتمل التحليل: ${result.move_count} نقلة · Stockfish · عمق ${result.depth}. استخدم التالي لمراجعة تقييم كل نقلة.`;
}

// ============================================================
// STOCKFISH ANALYSIS
// ============================================================

$('analyze').onclick = async () => {
  if (!selected?.pgn || !parsed?.moves?.length) {
    return;
  }

  $('analyze').disabled = true;

  $('moveList').innerHTML = '';

  analysisResult = null;

  renderBoard();

  renderMoveReview();

  $('bar').style.width = '0%';

  $('progressText').textContent =
    'جارٍ إرسال المباراة إلى محرك Stockfish على الخادم...';

  try {
    const started = await api('/api/analyze', {
      method: 'POST',

      headers: {
        'Content-Type': 'application/json'
      },

      body: JSON.stringify({
        pgn: selected.pgn,
        depth: 10,
        seconds: 0.12
      })
    });

    activeJob = started.job_id;

    while (true) {
      await new Promise(resolve =>
        setTimeout(resolve, 1200)
      );

      const job = await api(
        `/api/jobs/${activeJob}`
      );

      const total =
        job.total || parsed.moves.length;

      const progress =
        job.progress || 0;

      $('bar').style.width =
        (total ? progress / total * 100 : 0) + '%';

      $('progressText').textContent =
        job.message || 'جارٍ التحليل...';

      if (job.status === 'completed') {
        showAnalysis(job.result);
        break;
      }

      if (job.status === 'failed') {
        throw new Error(
          job.error || 'فشل التحليل.'
        );
      }
    }

  } catch (error) {
    $('progressText').textContent =
      'تعذر إكمال التحليل: ' + error.message;

  } finally {
    $('analyze').disabled = false;
  }
};
