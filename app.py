import io
import os
import re
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import chess
import chess.engine
import chess.pgn
import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 300 * 1024  # PGN requests are intentionally small.

CHESSCOM = 'https://api.chess.com/pub'
HEADERS = {
    'User-Agent': 'BASAM-Chess-Review/1.0 (personal chess analysis tool)',
    'Accept': 'application/json, application/x-chess-pgn, text/plain, */*',
}
ENGINE_PATH = os.environ.get('STOCKFISH_PATH') or shutil.which('stockfish') or '/usr/games/stockfish'
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='stockfish-analysis')
JOBS = {}
JOBS_LOCK = threading.Lock()
MAX_PLIES = 200


def chesscom_get(url, accept='application/json'):
    headers = dict(HEADERS)
    headers['Accept'] = accept
    try:
        response = requests.get(url, headers=headers, timeout=(8, 25))
    except requests.RequestException as exc:
        raise RuntimeError(f'تعذر الاتصال بـ Chess.com: {exc}') from exc
    if response.status_code == 404:
        raise RuntimeError('لم يتم العثور على البيانات المطلوبة في Chess.com.')
    if response.status_code == 429:
        raise RuntimeError('Chess.com طلب الانتظار قليلًا بسبب كثرة الطلبات. جرّب بعد دقيقة.')
    if not response.ok:
        raise RuntimeError(f'Chess.com أعاد الحالة {response.status_code}.')
    return response


@app.get('/')
def home():
    return send_from_directory('static', 'index.html')


@app.get('/health')
def health():
    engine_exists = os.path.isfile(ENGINE_PATH) and os.access(ENGINE_PATH, os.X_OK)
    return jsonify({
        'status': 'ok',
        'engine_found': engine_exists,
        'engine_path': ENGINE_PATH if engine_exists else None,
    }), (200 if engine_exists else 503)


@app.get('/api/player/<username>/archives')
def player_archives(username):
    if not re.fullmatch(r'[A-Za-z0-9_-]{2,25}', username):
        return jsonify(error='اسم المستخدم غير صالح.'), 400
    try:
        response = chesscom_get(f'{CHESSCOM}/player/{username}/games/archives')
        data = response.json()
        archives = (data.get('archives') or [])[-12:][::-1]
        return jsonify(archives=archives)
    except (RuntimeError, ValueError) as exc:
        return jsonify(error=str(exc)), 502


@app.get('/api/games')
def games_for_archive():
    archive = request.args.get('archive', '')
    parsed = urlparse(archive)
    if (parsed.scheme != 'https' or parsed.netloc != 'api.chess.com'
            or not re.fullmatch(r'/pub/player/[A-Za-z0-9_-]{2,25}/games/\d{4}/\d{2}', parsed.path)):
        return jsonify(error='رابط أرشيف المباريات غير صالح.'), 400
    try:
        response = chesscom_get(archive)
        data = response.json()
        return jsonify(games=(data.get('games') or [])[-300:])
    except (RuntimeError, ValueError) as exc:
        return jsonify(error=str(exc)), 502


def update_job(job_id, **values):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(values)


def analyse_game(job_id, pgn_text, depth, seconds_per_position):
    try:
        if not (os.path.isfile(ENGINE_PATH) and os.access(ENGINE_PATH, os.X_OK)):
            raise RuntimeError('لم يتم العثور على Stockfish في الخادم. تحقق من تثبيت المحرك أثناء النشر.')

        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if game is None:
            raise RuntimeError('ملف PGN فارغ أو غير صالح.')
        if game.errors:
            raise RuntimeError('ملف PGN يحتوي على أخطاء ولا يمكن تحليله بأمان.')

        moves = list(game.mainline_moves())
        if not moves:
            raise RuntimeError('المباراة لا تحتوي على نقلات.')
        if len(moves) > MAX_PLIES:
            raise RuntimeError(f'المباراة طويلة جدًا. الحد الحالي {MAX_PLIES} نصف نقلة.')

        boards = []
        board = game.board()
        boards.append(board.copy(stack=False))
        san_moves = []
        for move in moves:
            if move not in board.legal_moves:
                raise RuntimeError('تحتوي المباراة على نقلة غير قانونية في PGN.')
            san_moves.append(board.san(move))
            board.push(move)
            boards.append(board.copy(stack=False))

        total = len(boards)
        scores = [0.0] * total
        best_sans = ['—'] * total
        best_uci = [''] * total
        second_scores = [None] * total
        limit = chess.engine.Limit(time=seconds_per_position, depth=depth)
        update_job(job_id, status='running', progress=0, total=len(moves), message='تم تجهيز المباراة. يبدأ Stockfish الآن.')

        engine = chess.engine.SimpleEngine.popen_uci(ENGINE_PATH, timeout=15)
        try:
            options = {}
            if 'Threads' in engine.options:
                options['Threads'] = 1
            if 'Hash' in engine.options:
                options['Hash'] = 32
            if options:
                engine.configure(options)

            for index, position in enumerate(boards):
                if position.is_checkmate():
                    scores[index] = -100.0
                elif position.is_game_over(claim_draw=True):
                    scores[index] = 0.0
                else:
                    # MultiPV gives a second-best line so 'Great' can be reserved
                    # for positions where the played top move is clearly critical.
                    infos = engine.analyse(position, limit, multipv=2)
                    if isinstance(infos, dict):
                        infos = [infos]
                    info = infos[0]
                    pov_score = info['score'].pov(position.turn)
                    raw_score = pov_score.score(mate_score=10000)
                    scores[index] = (raw_score or 0) / 100.0
                    pv = info.get('pv') or []
                    if pv:
                        best_uci[index] = pv[0].uci()
                        try:
                            best_sans[index] = position.san(pv[0])
                        except (ValueError, AssertionError):
                            best_sans[index] = pv[0].uci()
                    if len(infos) > 1:
                        second_raw = infos[1]['score'].pov(position.turn).score(mate_score=10000)
                        second_scores[index] = (second_raw or 0) / 100.0
                if index < len(moves):
                    update_job(job_id, progress=index + 1,
                               message=f'تحليل الوضعية {index + 1} من {len(moves)}')
        finally:
            engine.quit()

        rows = []
        blunders = mistakes = inaccuracies = 0
        total_loss = 0.0
        piece_values = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
        # A small, conservative set of common first moves. This is only used
        # to mark unmistakable early opening theory, not to pretend we have a full ECO book.
        common_book_moves = {'e2e4','d2d4','c2c4','g1f3','e7e5','c7c5','e7e6','c7c6','d7d5','g8f6','d7d6','g7g6','b8c6','b8d7','f8c5','f8e7','f8b4','g8e7'}
        for index, san in enumerate(san_moves):
            # Each score is from the side-to-move perspective. The next position
            # belongs to the opponent, so negate it to compare from mover's view.
            loss = max(0.0, scores[index] + scores[index + 1])
            played = moves[index]
            mover = boards[index].turn
            after_for_mover = -scores[index + 1]
            is_best = bool(best_uci[index]) and played.uci() == best_uci[index]
            second = second_scores[index]
            best_gap = (scores[index] - second) if second is not None else 0.0

            # Conservative tactical heuristics for the special Chess.com-style labels.
            moved_piece = boards[index].piece_at(played.from_square)
            after_board = boards[index + 1]
            sacrifice = False
            if moved_piece and piece_values.get(moved_piece.piece_type, 0) >= 3:
                target = played.to_square
                attacked = after_board.is_attacked_by(not mover, target)
                defended = after_board.is_attacked_by(mover, target)
                sacrifice = attacked and not defended

            if scores[index] >= 2.5 and after_for_mover < 1.0 and loss >= 0.8:
                label, category = 'تفويت فرصة', 'miss'
            elif (is_best and sacrifice and loss <= 0.15
                  and scores[index] < 2.5 and after_for_mover >= -0.5):
                label, category = 'عبقرية', 'brilliant'
            elif is_best and (best_gap >= 0.8 or
                              (scores[index] <= -1.5 and after_for_mover >= -0.3) or
                              (scores[index] <= 0.3 and after_for_mover >= 1.5)):
                label, category = 'رائعة', 'great'
            elif index < 8 and played.uci() in common_book_moves and loss <= 0.12:
                label, category = 'افتتاحية', 'book'
            elif loss <= 0.02:
                label, category = 'الأفضل', 'best'
            elif loss <= 0.10:
                label, category = 'ممتازة', 'excellent'
            elif loss <= 0.25:
                label, category = 'جيدة', 'good'
            elif loss <= 0.50:
                label, category = 'غير دقيقة', 'inaccuracy'
                inaccuracies += 1
            elif loss <= 1.50:
                label, category = 'خطأ', 'mistake'
                mistakes += 1
            else:
                label, category = 'خطأ فادح', 'blunder'
                blunders += 1
            total_loss += min(loss, 5.0)
            rows.append({
                'ply': index + 1,
                'san': san,
                'uci': played.uci(),
                'best': best_sans[index],
                'loss': round(loss, 2),
                'label': label,
                'category': category,
                'fen': boards[index + 1].fen(),
            })

        accuracy = max(0, round(100 - (total_loss / (len(moves) * 5) * 100)))
        result = {
            'moves': rows,
            'blunders': blunders,
            'mistakes': mistakes,
            'inaccuracies': inaccuracies,
            'accuracy': accuracy,
            'move_count': len(moves),
            'depth': depth,
            'engine': 'Stockfish (server-side)',
        }
        update_job(job_id, status='completed', progress=len(moves), result=result,
                   message='اكتمل التحليل بنجاح.')
    except Exception as exc:
        update_job(job_id, status='failed', error=str(exc), message='تعذر إكمال التحليل.')


@app.post('/api/parse')
def parse_game():
    payload = request.get_json(silent=True) or {}
    pgn_text = payload.get('pgn', '')
    if not isinstance(pgn_text, str) or not pgn_text.strip():
        return jsonify(error='لم يصل PGN صالح للمباراة.'), 400
    if len(pgn_text) > 250_000:
        return jsonify(error='ملف المباراة أكبر من الحد المسموح.'), 413
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None or game.errors:
        return jsonify(error='ملف PGN فارغ أو غير صالح.'), 400
    board = game.board()
    positions = [board.fen()]
    rows = []
    for move in game.mainline_moves():
        if move not in board.legal_moves:
            return jsonify(error='تحتوي المباراة على نقلة غير قانونية.'), 400
        san = board.san(move)
        board.push(move)
        rows.append({'san': san, 'uci': move.uci()})
        positions.append(board.fen())
        if len(rows) > MAX_PLIES:
            return jsonify(error=f'المباراة أطول من الحد الحالي ({MAX_PLIES} نصف نقلة).'), 400
    return jsonify(headers={k: game.headers.get(k, '') for k in ('White', 'Black', 'Date', 'Result', 'TimeControl', 'WhiteElo', 'BlackElo')},
                   positions=positions, moves=rows)


@app.post('/api/analyze')
def start_analysis():
    payload = request.get_json(silent=True) or {}
    pgn_text = payload.get('pgn', '')
    if not isinstance(pgn_text, str) or not pgn_text.strip():
        return jsonify(error='لم يصل PGN صالح للمباراة.'), 400
    if len(pgn_text) > 250_000:
        return jsonify(error='ملف المباراة أكبر من الحد المسموح.'), 413
    try:
        depth = int(payload.get('depth', 10))
        seconds = float(payload.get('seconds', 0.12))
    except (TypeError, ValueError):
        return jsonify(error='إعدادات التحليل غير صالحة.'), 400
    depth = max(8, min(depth, 14))
    seconds = max(0.08, min(seconds, 0.35))
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {'status': 'queued', 'progress': 0, 'total': 0,
                        'message': 'تم وضع المباراة في قائمة التحليل.'}
    POOL.submit(analyse_game, job_id, pgn_text, depth, seconds)
    return jsonify(job_id=job_id), 202


@app.get('/api/jobs/<job_id>')
def job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify(error='لم يتم العثور على مهمة التحليل. ربما أعيد تشغيل الخادم.'), 404
        return jsonify(job)


@app.errorhandler(413)
def too_large(_error):
    return jsonify(error='الطلب أكبر من الحد المسموح.'), 413


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
