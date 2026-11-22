
# ============================================================
# BASAM v11 — Chess Review & Stockfish Analysis
# ============================================================

import os
import io
import uuid
import time
import shutil
import logging
import threading
import requests
import chess
import chess.pgn
import chess.engine

from flask import Flask, request, jsonify
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ---------------- CONFIG ----------------

MAX_PLIES = 200
MAX_WORKERS = 1

DEFAULT_DEPTH = 14
DEFAULT_SECONDS = 0.35

MAX_DEPTH = 18
MAX_SECONDS = 1.0

STOCKFISH_PATH = (
    os.environ.get("STOCKFISH_PATH")
    or shutil.which("stockfish")
    or "/usr/games/stockfish"
)

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
jobs = {}
jobs_lock = threading.Lock()

CHESS_HEADERS = {
    "User-Agent": "BASAM-Chess-Review/11.0"
}

# ---------------- BASIC ROUTES ----------------

@app.route("/")
def home():
    return jsonify({
        "name": "BASAM Chess Review",
        "version": "11.0",
        "status": "online"
    })


@app.route("/health")
def health():
    engine_exists = (
        os.path.isfile(STOCKFISH_PATH)
        or shutil.which(STOCKFISH_PATH) is not None
    )

    return jsonify({
        "status": "ok",
        "version": "11.0",
        "engine": "Stockfish",
        "engine_path": STOCKFISH_PATH,
        "engine_available": engine_exists
    })


# ---------------- CHESS.COM API ----------------

def chesscom_get(url):
    response = requests.get(
        url,
        headers=CHESS_HEADERS,
        timeout=25
    )
    response.raise_for_status()
    return response.json()


@app.route("/api/player/<username>/archives")
def player_archives(username):
    username = username.strip()

    if not username:
        return jsonify({"error": "Username is required"}), 400

    try:
        data = chesscom_get(
            f"https://api.chess.com/pub/player/{username}/games/archives"
        )

        return jsonify({
            "username": username,
            "archives": data.get("archives", [])
        })

    except requests.HTTPError as e:
        status = e.response.status_code if e.response else 502

        return jsonify({
            "error": "Unable to retrieve player archives",
            "details": str(e)
        }), status

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/games", methods=["GET", "POST"])
def get_games():
    data = request.get_json(silent=True) or {}

    archive_url = (
        request.args.get("url")
        or request.args.get("archive")
        or data.get("url")
        or data.get("archive")
        or data.get("archive_url")
    )

    if not archive_url:
        return jsonify({
            "error": "Archive URL is required"
        }), 400

    if not archive_url.startswith(
        "https://api.chess.com/pub/player/"
    ):
        return jsonify({
            "error": "Invalid Chess.com archive URL"
        }), 400

    try:
        result = chesscom_get(archive_url)

        return jsonify({
            "games": result.get("games", [])
        })

    except Exception as e:
        return jsonify({
            "error": "Unable to retrieve games",
            "details": str(e)
        }), 502


# ---------------- PGN PARSING ----------------

def parse_pgn_text(pgn_text):
    if not isinstance(pgn_text, str) or not pgn_text.strip():
        raise ValueError("PGN is empty")

    pgn_io = io.StringIO(pgn_text)
    game = chess.pgn.read_game(pgn_io)

    if game is None:
        raise ValueError("Unable to read PGN")

    if game.errors:
        raise ValueError(
            "PGN contains errors: " +
            "; ".join(str(e) for e in game.errors)
        )

    board = game.board()
    moves = []
    positions = [board.fen()]

    for move in game.mainline_moves():
        if len(moves) >= MAX_PLIES:
            break

        if move not in board.legal_moves:
            raise ValueError("PGN contains an illegal move")

        san = board.san(move)
        uci = move.uci()

        board.push(move)

        moves.append({
            "ply": len(moves) + 1,
            "san": san,
            "uci": uci
        })

        positions.append(board.fen())

    return game, moves, positions


@app.route("/api/parse", methods=["POST"])
def parse_game():
    data = request.get_json(silent=True) or {}

    pgn_text = (
        data.get("pgn")
        or data.get("game")
        or data.get("text")
    )

    try:
        game, moves, positions = parse_pgn_text(pgn_text)

        return jsonify({
            "headers": dict(game.headers),
            "moves": moves,
            "positions": positions,
            "move_count": len(moves)
        })

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 400


# ---------------- ENGINE HELPERS ----------------

def get_engine_path():
    path = STOCKFISH_PATH

    if os.path.isfile(path):
        return path

    found = shutil.which(path)

    if found:
        return found

    raise RuntimeError(
        "Stockfish was not found. "
        "Set STOCKFISH_PATH to the Stockfish executable."
    )


def score_to_cp(score):
    """
    Convert a python-chess PovScore into a comparable
    centipawn-like number.

    Positive = advantage for the requested side.
    Negative = disadvantage for the requested side.
    """

    if score is None:
        return 0

    value = score.score(mate_score=100000)

    if value is None:
        return 0

    return int(value)


def classify_move(loss_cp, is_best):
    """
    BASAM classification thresholds.

    loss_cp is measured in centipawns.
    """

    if loss_cp <= 10:
        if is_best:
            return {
                "label": "الأفضل",
                "category": "best",
                "symbol": "★"
            }

        return {
            "label": "ممتازة",
            "category": "excellent",
            "symbol": "!"
        }

    if loss_cp <= 25:
        return {
            "label": "ممتازة",
            "category": "excellent",
            "symbol": "!"
        }

    if loss_cp <= 50:
        return {
            "label": "جيدة",
            "category": "good",
            "symbol": "✓"
        }

    if loss_cp <= 100:
        return {
            "label": "غير دقيقة",
            "category": "inaccuracy",
            "symbol": "?!"
        }

    if loss_cp <= 200:
        return {
            "label": "خطأ",
            "category": "mistake",
            "symbol": "?"
        }

    return {
        "label": "خطأ فادح",
        "category": "blunder",
        "symbol": "??"
    }


def calculate_accuracy(losses):
    """
    BASAM accuracy estimate.
    This is not Chess.com's official accuracy formula.
    """

    if not losses:
        return 100

    total = sum(min(max(x, 0), 1000) for x in losses)
    average = total / len(losses)

    # Smooth penalty curve.
    accuracy = 100 * (2.718281828 ** (-average / 350))

    return round(max(0, min(100, accuracy)), 1)


# ---------------- SINGLE MOVE ANALYSIS ----------------

def analyze_one_move(engine, board, played_move, depth, seconds):
    """
    Compare:
      1. Best engine move from the current position.
      2. The move actually played from that same position.

    Both evaluations use the side-to-move perspective.
    """

    mover = board.turn
    limit = chess.engine.Limit(
        depth=depth,
        time=seconds
    )

    # First: unrestricted best move.
    best_info = engine.analyse(
        board,
        limit
    )

    best_score_obj = best_info.get("score")
    best_score = score_to_cp(
        best_score_obj.pov(mover)
        if best_score_obj else None
    )

    best_pv = best_info.get("pv", [])
    best_move = best_pv[0] if best_pv else None

    best_san = (
        board.san(best_move)
        if best_move and best_move in board.legal_moves
        else "—"
    )

    # Second: force Stockfish to evaluate the played move.
    played_info = engine.analyse(
        board,
        limit,
        root_moves=[played_move]
    )

    played_score_obj = played_info.get("score")
    played_score = score_to_cp(
        played_score_obj.pov(mover)
        if played_score_obj else None
    )

    # Difference between the best choice and the played move.
    loss_cp = max(0, best_score - played_score)

    is_best = (
        best_move is not None
        and played_move == best_move
    )

    classification = classify_move(
        loss_cp,
        is_best
    )

    return {
        "best": best_san,
        "best_uci": best_move.uci() if best_move else None,
        "played_eval": round(played_score / 100, 2),
        "best_eval": round(best_score / 100, 2),
        "centipawn_loss": loss_cp,
        "loss": round(loss_cp / 100, 2),
        "is_best": is_best,
        **classification
    }


# ---------------- FULL GAME ANALYSIS ----------------

def run_analysis(job_id, pgn_text, depth, seconds):
    try:
        game, parsed_moves, positions = parse_pgn_text(pgn_text)

        if not parsed_moves:
            raise ValueError("The game contains no moves")

        engine_path = get_engine_path()

        results = []
        losses = []

        blunders = 0
        mistakes = 0
        inaccuracies = 0

        with chess.engine.SimpleEngine.popen_uci(
            engine_path,
            timeout=max(30, seconds * 20)
        ) as engine:

            try:
                engine.configure({
                    "Threads": 1,
                    "Hash": 128
                })
            except Exception:
                pass

            board = game.board()

            for index, played_move in enumerate(
                list(game.mainline_moves())[:MAX_PLIES]
            ):

                if played_move not in board.legal_moves:
                    raise ValueError(
                        f"Illegal move at ply {index + 1}"
                    )

                san = board.san(played_move)
                uci = played_move.uci()
                fen_before = board.fen()

                evaluation = analyze_one_move(
                    engine,
                    board,
                    played_move,
                    depth,
                    seconds
                )

                category = evaluation["category"]

                if category == "blunder":
                    blunders += 1

                elif category == "mistake":
                    mistakes += 1

                elif category == "inaccuracy":
                    inaccuracies += 1

                result = {
                    "ply": index + 1,
                    "san": san,
                    "uci": uci,
                    "fen": fen_before,

                    "best": evaluation["best"],
                    "best_uci": evaluation["best_uci"],

                    "loss": evaluation["loss"],
                    "centipawn_loss": evaluation["centipawn_loss"],

                    "played_eval": evaluation["played_eval"],
                    "best_eval": evaluation["best_eval"],

                    "is_best": evaluation["is_best"],

                    "label": evaluation["label"],
                    "category": category,
                    "symbol": evaluation["symbol"]
                }

                results.append(result)
                losses.append(
                    evaluation["centipawn_loss"]
                )

                board.push(played_move)

                # Update progress.
                with jobs_lock:
                    if job_id in jobs:
                        jobs[job_id]["progress"] = round(
                            (index + 1) / len(parsed_moves) * 100,
                            1
                        )

                        jobs[job_id]["current_move"] = index + 1
                        jobs[job_id]["move_count"] = len(parsed_moves)

                        # Make completed moves available immediately.
                        jobs[job_id]["partial_moves"] = results.copy()

        accuracy = calculate_accuracy(losses)

        final_result = {
            "success": True,
            "version": "11.0",

            "moves": results,

            "blunders": blunders,
            "mistakes": mistakes,
            "inaccuracies": inaccuracies,

            "accuracy": accuracy,
            "move_count": len(results),

            "depth": depth,
            "seconds": seconds,
            "engine": "Stockfish",

            "accuracy_note": (
                "BASAM accuracy estimate; "
                "not the official Chess.com accuracy formula."
            ),

            "headers": dict(game.headers)
        }

        with jobs_lock:
            jobs[job_id].update({
                "status": "completed",
                "progress": 100,
                "result": final_result,
                "error": None
            })

    except Exception as e:
        logging.exception("Analysis failed")

        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update({
                    "status": "failed",
                    "error": str(e)
                })


# ---------------- START ANALYSIS ----------------

@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}

    pgn_text = (
        data.get("pgn")
        or data.get("game")
        or data.get("text")
    )

    if not isinstance(pgn_text, str) or not pgn_text.strip():
        return jsonify({
            "error": "PGN is required"
        }), 400

    try:
        depth = int(data.get("depth", DEFAULT_DEPTH))
        seconds = float(data.get("seconds", DEFAULT_SECONDS))

        depth = max(8, min(depth, MAX_DEPTH))
        seconds = max(0.1, min(seconds, MAX_SECONDS))

        # Validate PGN before creating the job.
        game, parsed_moves, _ = parse_pgn_text(pgn_text)

        if not parsed_moves:
            return jsonify({
                "error": "No moves found in PGN"
            }), 400

        job_id = str(uuid.uuid4())

        with jobs_lock:
            jobs[job_id] = {
                "status": "queued",
                "progress": 0,
                "current_move": 0,
                "move_count": len(parsed_moves),
                "partial_moves": [],
                "result": None,
                "error": None,
                "created_at": time.time()
            }

        executor.submit(
            run_analysis,
            job_id,
            pgn_text,
            depth,
            seconds
        )

        return jsonify({
            "success": True,
            "job_id": job_id,
            "status": "queued",
            "move_count": len(parsed_moves),
            "depth": depth,
            "seconds": seconds,
            "version": "11.0"
        })

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 400


# ---------------- JOB STATUS ----------------

@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)

        if not job:
            return jsonify({
                "error": "Job not found"
            }), 404

        response = {
            "job_id": job_id,
            "status": job["status"],
            "progress": job["progress"],
            "current_move": job["current_move"],
            "move_count": job["move_count"],
            "partial_moves": job["partial_moves"],
            "error": job["error"]
        }

        if job["status"] == "completed":
            response["result"] = job["result"]

        return jsonify(response)


# ---------------- ERROR HANDLERS ----------------

@app.errorhandler(404)
def not_found(error):
    return jsonify({
        "error": "Endpoint not found"
    }), 404


@app.errorhandler(500)
def server_error(error):
    return jsonify({
        "error": "Internal server error"
    }), 500


# ---------------- START SERVER ----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )
