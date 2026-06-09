import os
import json
import math
import hashlib
import subprocess
from pathlib import Path
from flask import Flask, render_template, request, jsonify
import chess
import chess.pgn
import chess.engine
from io import StringIO

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "analysis_cache"
CACHE_DIR.mkdir(exist_ok=True)

STOCKFISH_CANDIDATES = [
    os.environ.get("STOCKFISH_PATH", ""),
    str(BASE_DIR / "bin" / "stockfish" / "stockfish-ubuntu-x86-64"),
    str(BASE_DIR / "bin" / "stockfish"),
    "/usr/bin/stockfish",
    "/usr/games/stockfish",
    "stockfish",
]

PIECE_VALUE = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}

CAT_META = {
    "book": {"label": "Книжный", "icon": "📖", "short": "📖"},
    "brilliant": {"label": "Блестящий", "icon": "!!", "short": "!!"},
    "great": {"label": "Замечательный", "icon": "!", "short": "!"},
    "best": {"label": "Лучший", "icon": "★", "short": "★"},
    "excellent": {"label": "Отличный", "icon": "✓", "short": "✓"},
    "good": {"label": "Хороший", "icon": "•", "short": "•"},
    "inaccuracy": {"label": "Неточность", "icon": "?!", "short": "?!"},
    "mistake": {"label": "Ошибка", "icon": "?", "short": "?"},
    "miss": {"label": "Упущенная возможность", "icon": "✕", "short": "✕"},
    "blunder": {"label": "Зевок", "icon": "??", "short": "??"},
}

OPENING_PLY_LIMIT = 18
MATE_SCORE = 100000


def find_stockfish():
    for p in STOCKFISH_CANDIDATES:
        if not p:
            continue
        try:
            if p == "stockfish":
                r = subprocess.run([p], input="uci\nquit\n", text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
                if "uciok" in r.stdout:
                    return p
            else:
                path = Path(p)
                if path.exists():
                    try:
                        os.chmod(path, os.stat(path).st_mode | 0o111)
                    except Exception:
                        pass
                    r = subprocess.run([str(path)], input="uci\nquit\n", text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
                    if "uciok" in r.stdout:
                        return str(path)
        except Exception:
            continue
    return None


def parse_pgn(pgn_text):
    pgn = StringIO(pgn_text.strip())
    game = chess.pgn.read_game(pgn)
    if game is None:
        raise ValueError("PGN не распознан")
    board = game.board()
    moves = []
    fens = [board.fen()]
    headers = dict(game.headers)
    for move in game.mainline_moves():
        color = "w" if board.turn == chess.WHITE else "b"
        san = board.san(move)
        uci = move.uci()
        before_fen = board.fen()
        board.push(move)
        after_fen = board.fen()
        moves.append({
            "san": san,
            "uci": uci,
            "from": chess.square_name(move.from_square),
            "to": chess.square_name(move.to_square),
            "piece": chess.piece_symbol(board.piece_at(move.to_square).piece_type).upper() if board.piece_at(move.to_square) else "",
            "color": color,
            "before_fen": before_fen,
            "after_fen": after_fen,
            "ply": len(moves) + 1,
        })
        fens.append(after_fen)
    if not moves:
        raise ValueError("В PGN нет ходов")
    return headers, moves, fens


def material(board, color):
    s = 0
    for sq, piece in board.piece_map().items():
        if piece.color == color:
            s += PIECE_VALUE.get(piece.piece_type, 0)
    return s


def is_capture_or_tactical(board, move):
    return board.is_capture(move) or board.gives_check(move) or move.promotion is not None


def score_to_cp(score, pov_color):
    pov = score.pov(pov_color)
    if pov.is_mate():
        m = pov.mate()
        if m is None:
            return 0
        return MATE_SCORE if m > 0 else -MATE_SCORE
    v = pov.score(mate_score=MATE_SCORE)
    return int(v or 0)


def safe_engine_analyse(engine, board, depth):
    # depth-based deterministic enough; one thread/hash fixed in engine options.
    info = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=1)[0]
    score = info.get("score")
    pv = info.get("pv", [])
    best_move = pv[0] if pv else None
    return score, best_move, pv


def classify_move(board_before, move, best_move, best_score_for_mover, after_score_for_mover, ply):
    mover = board_before.turn
    played_uci = move.uci()
    best_uci = best_move.uci() if best_move else ""
    loss = max(0, best_score_for_mover - after_score_for_mover)
    gain = after_score_for_mover - best_score_for_mover

    own_before = material(board_before, mover)
    opp_before = material(board_before, not mover)
    captured = board_before.is_capture(move)
    tactical = is_capture_or_tactical(board_before, move)
    gives_check = board_before.gives_check(move)

    tmp = board_before.copy()
    tmp.push(move)
    own_after = material(tmp, mover)
    opp_after = material(tmp, not mover)
    own_drop = own_before - own_after
    opp_drop = opp_before - opp_after

    is_best = (best_uci == played_uci) or loss <= 8
    sacrifice = own_drop >= 300 and opp_drop < own_drop

    # Opening/book approximation. Chess.com uses a database; here we avoid calling random engine bests "book" deep into the game.
    if ply <= OPENING_PLY_LIMIT and loss <= 18 and not gives_check and not captured:
        return "book", loss

    # Missed opportunity: position was clearly good/winning, played move lost a large chunk.
    if best_score_for_mover >= 250 and loss >= 130:
        if loss >= 280:
            return "blunder", loss
        return "miss", loss

    if sacrifice and loss <= 25 and (tactical or gives_check or after_score_for_mover >= best_score_for_mover - 25):
        return "brilliant", loss

    if is_best and (tactical or gives_check) and after_score_for_mover >= best_score_for_mover - 15:
        if loss <= 12 and (captured or gives_check or abs(after_score_for_mover) >= 180):
            return "great", loss

    if is_best:
        return "best", loss
    if loss <= 22:
        return "excellent", loss
    if loss <= 55:
        return "good", loss
    if loss <= 110:
        return "inaccuracy", loss
    if loss <= 240:
        return "mistake", loss
    return "blunder", loss


def accuracy_from_losses(losses):
    if not losses:
        return 0.0
    # Smooth Chess.com-like estimate: small losses stay high, large losses punish heavily.
    avg_loss = sum(min(x, 1000) for x in losses) / len(losses)
    acc = 103.0 * math.exp(-avg_loss / 280.0) - 3.0
    return round(max(0.0, min(99.9, acc)), 1)


def game_rating_from_accuracy(acc, loss_count):
    # Approximate report rating, not official. Punishes errors; keeps stable across devices.
    base = 400 + acc * 25
    penalty = min(450, loss_count * 45)
    return int(max(100, min(3300, round((base - penalty) / 5) * 5)))


def analyze_game(pgn_text, depth):
    headers, moves, fens = parse_pgn(pgn_text)
    sf = find_stockfish()
    if not sf:
        raise RuntimeError("Stockfish не найден. Укажи STOCKFISH_PATH или установи движок.")

    result_key = hashlib.sha256((pgn_text.strip() + f"|depth={depth}|v4.2").encode("utf-8")).hexdigest()
    cache_file = CACHE_DIR / f"{result_key}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    analysed_moves = []
    losses = {"w": [], "b": []}
    counts = {c: {k: 0 for k in CAT_META} for c in ("w", "b")}
    evals_white = []

    engine = chess.engine.SimpleEngine.popen_uci(sf)
    try:
        try:
            engine.configure({"Threads": 1, "Hash": 64})
        except Exception:
            pass

        board = chess.Board()
        for i, m in enumerate(moves):
            board = chess.Board(m["before_fen"])
            move = chess.Move.from_uci(m["uci"])
            mover = board.turn

            before_score_obj, best_move, pv = safe_engine_analyse(engine, board, depth)
            best_score_mover = score_to_cp(before_score_obj, mover)
            eval_before_white = score_to_cp(before_score_obj, chess.WHITE)

            board_after = board.copy()
            board_after.push(move)
            after_score_obj, _, _ = safe_engine_analyse(engine, board_after, depth)
            after_score_mover = score_to_cp(after_score_obj, mover)
            eval_after_white = score_to_cp(after_score_obj, chess.WHITE)

            cat, loss = classify_move(board, move, best_move, best_score_mover, after_score_mover, m["ply"])
            color_key = "w" if mover == chess.WHITE else "b"
            losses[color_key].append(loss)
            counts[color_key][cat] += 1
            evals_white.append(eval_after_white)

            best_san = None
            if best_move:
                try:
                    best_san = board.san(best_move)
                except Exception:
                    best_san = best_move.uci()

            line_san = []
            pv_board = board.copy()
            for pvm in pv[:6]:
                try:
                    line_san.append(pv_board.san(pvm))
                    pv_board.push(pvm)
                except Exception:
                    break

            item = dict(m)
            item.update({
                "category": cat,
                "category_label": CAT_META[cat]["label"],
                "category_icon": CAT_META[cat]["short"],
                "loss": int(round(loss)),
                "best_uci": best_move.uci() if best_move else "",
                "best_san": best_san or "—",
                "eval_before_white": eval_before_white,
                "eval_after_white": eval_after_white,
                "score_for_mover_before": best_score_mover,
                "score_for_mover_after": after_score_mover,
                "pv": " ".join(line_san),
            })
            analysed_moves.append(item)
    finally:
        try:
            engine.quit()
        except Exception:
            pass

    acc_w = accuracy_from_losses(losses["w"])
    acc_b = accuracy_from_losses(losses["b"])
    serious_w = counts["w"]["inaccuracy"] + counts["w"]["mistake"] + counts["w"]["miss"] + counts["w"]["blunder"]
    serious_b = counts["b"]["inaccuracy"] + counts["b"]["mistake"] + counts["b"]["miss"] + counts["b"]["blunder"]

    report = {
        "ok": True,
        "version": "v4.2-server-deterministic",
        "depth": depth,
        "stockfish_path": sf,
        "headers": {
            "white": headers.get("White", "White"),
            "black": headers.get("Black", "Black"),
            "event": headers.get("Event", ""),
            "date": headers.get("Date", ""),
            "result": headers.get("Result", ""),
        },
        "summary": {
            "accuracy": {"w": acc_w, "b": acc_b},
            "rating": {"w": game_rating_from_accuracy(acc_w, serious_w), "b": game_rating_from_accuracy(acc_b, serious_b)},
            "counts": counts,
            "categories": CAT_META,
        },
        "moves": analysed_moves,
        "evals_white": evals_white,
    }
    cache_file.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return report


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    sf = find_stockfish()
    return jsonify({"ok": bool(sf), "stockfish": sf or None})


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data = request.get_json(silent=True) or {}
    pgn = data.get("pgn", "")
    depth = int(data.get("depth", 14))
    depth = max(8, min(22, depth))
    try:
        report = analyze_game(pgn, depth)
        return jsonify(report)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
