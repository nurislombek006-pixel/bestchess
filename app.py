import hashlib
import io
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, request
import chess
import chess.engine
import chess.pgn

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# Put your exact Stockfish path here if auto-detect fails.
STOCKFISH_CANDIDATES = [
    os.environ.get("STOCKFISH_PATH", ""),
    "stockfish",
    "stockfish.exe",
    r"C:\\stockfish\\stockfish.exe",
    r"C:\\Program Files\\Stockfish\\stockfish.exe",
    r"C:\\Users\\USER\\Downloads\\stockfish\\stockfish-windows-x86-64-avx2.exe",
    r"/usr/bin/stockfish",
    r"/usr/local/bin/stockfish",
    r"/opt/homebrew/bin/stockfish",
]

app = Flask(__name__)

PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}

BOOK_COMMON_UCI = {
    # A compact opening sanity list. This is not a full book; it prevents obvious early theory from being called dry.
    "e2e4", "d2d4", "c2c4", "g1f3", "c2c3", "g2g3", "b2b3", "f2f4",
    "e7e5", "c7c5", "e7e6", "c7c6", "d7d5", "g8f6", "g7g6", "d7d6",
    "g1f3", "b1c3", "f1c4", "f1b5", "d2d4", "c2c4", "c2c3",
    "b8c6", "g8f6", "f8c5", "f8b4", "a7a6", "d7d6", "e7e6",
    "e1g1", "e8g8", "d1e2", "d8e7", "c1g5", "c8g4",
}


def find_stockfish() -> Optional[str]:
    for candidate in STOCKFISH_CANDIDATES:
        if not candidate:
            continue
        found = shutil.which(candidate) if os.path.basename(candidate) == candidate else candidate
        if found and Path(found).exists() or shutil.which(candidate):
            return found if Path(str(found)).exists() else shutil.which(candidate)
    return None


def parse_game(pgn_text: str) -> chess.pgn.Game:
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise ValueError("PGN не распознан. Вставь полный PGN партии.")
    return game


def material(board: chess.Board, color: chess.Color) -> int:
    total = 0
    for piece_type, val in PIECE_VALUES.items():
        total += len(board.pieces(piece_type, color)) * val
    return total


def material_balance_for(board: chess.Board, color: chess.Color) -> int:
    return material(board, color) - material(board, not color)


def score_white(info: Dict[str, Any]) -> int:
    score = info.get("score")
    if score is None:
        return 0
    return int(score.white().score(mate_score=100000))


def score_for_color(score_w: int, color: chess.Color) -> int:
    return score_w if color == chess.WHITE else -score_w


def score_text(score_w: int) -> str:
    if abs(score_w) >= 90000:
        return "M" + ("+" if score_w > 0 else "-")
    return f"{score_w / 100:.2f}"


def pv_to_san(board: chess.Board, pv: List[chess.Move], max_len: int = 8) -> str:
    b = board.copy(stack=False)
    out = []
    for mv in pv[:max_len]:
        if mv not in b.legal_moves:
            break
        out.append(b.san(mv))
        b.push(mv)
    return " ".join(out)


def is_tactical(board: chess.Board, move: chess.Move) -> bool:
    san = board.san(move)
    return board.is_capture(move) or move.promotion is not None or "+" in san or "#" in san


def is_sacrifice(before: chess.Board, move: chess.Move) -> bool:
    color = before.turn
    bal_before = material_balance_for(before, color)
    after = before.copy(stack=False)
    after.push(move)
    bal_after = material_balance_for(after, color)
    # Immediate material sacrifice: material balance worsens by around a minor piece or more.
    return (bal_after - bal_before) <= -250


def phase_for_ply(ply: int, board: chess.Board) -> str:
    non_pawn_material = 0
    queens = 0
    for color in [chess.WHITE, chess.BLACK]:
        for piece_type in [chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN]:
            count = len(board.pieces(piece_type, color))
            non_pawn_material += count * PIECE_VALUES[piece_type]
            if piece_type == chess.QUEEN:
                queens += count
    if ply <= 16:
        return "opening"
    if queens <= 1 or non_pawn_material <= 2600:
        return "endgame"
    return "middlegame"


def classify_move(
    board_before: chess.Board,
    move: chess.Move,
    best_move: Optional[chess.Move],
    score_before_w: int,
    score_after_w: int,
    ply_index: int,
) -> Dict[str, Any]:
    color = board_before.turn
    san = board_before.san(move)
    best_san = board_before.san(best_move) if best_move and best_move in board_before.legal_moves else "—"
    played_uci = move.uci()
    best_uci = best_move.uci() if best_move else ""

    before_player = score_for_color(score_before_w, color)
    after_player = score_for_color(score_after_w, color)
    loss = max(0, before_player - after_player)
    best_played = bool(best_move and move == best_move)
    tactical = is_tactical(board_before, move)
    sacrifice = is_sacrifice(board_before, move)
    phase = phase_for_ply(ply_index + 1, board_before)

    # Chess.com-like deterministic labels. Not proprietary 1:1, but consistent and explainable.
    if phase == "opening" and played_uci in BOOK_COMMON_UCI and loss <= 25 and not board_before.gives_check(move):
        cat = "book"
    elif best_played and sacrifice and tactical and loss <= 35:
        cat = "brilliant"
    elif best_played and tactical and (after_player >= before_player - 10):
        cat = "great"
    elif best_played or loss <= 12:
        cat = "best"
    elif loss <= 35:
        cat = "excellent"
    elif loss <= 70:
        cat = "good"
    elif before_player >= 250 and loss >= 120:
        cat = "miss"
    elif loss >= 300:
        cat = "blunder"
    elif loss >= 130:
        cat = "mistake"
    else:
        cat = "inaccuracy"

    reason_map = {
        "book": "известный дебютный ход",
        "brilliant": "сильная жертва или тактика без ухудшения позиции",
        "great": "сильный тактический ход",
        "best": "лучший ход движка или почти без потери",
        "excellent": "отличный ход с маленькой потерей",
        "good": "нормальный ход, позиция почти не ухудшилась",
        "inaccuracy": "неточность, позиция стала хуже",
        "mistake": "ошибка, заметная потеря оценки",
        "miss": "упущена сильная возможность",
        "blunder": "зевок, большая потеря оценки",
    }

    return {
        "category": cat,
        "label": {
            "book": "Книжный",
            "brilliant": "Блестящий",
            "great": "Замечательный",
            "best": "Лучший",
            "excellent": "Отличный",
            "good": "Хороший",
            "inaccuracy": "Неточность",
            "mistake": "Ошибка",
            "miss": "Упущенная возможность",
            "blunder": "Зевок",
        }[cat],
        "icon": {
            "book": "📖",
            "brilliant": "!!",
            "great": "!",
            "best": "★",
            "excellent": "✓",
            "good": "•",
            "inaccuracy": "?!",
            "mistake": "?",
            "miss": "✕",
            "blunder": "??",
        }[cat],
        "loss_cp": int(round(loss)),
        "played_uci": played_uci,
        "best_uci": best_uci,
        "san": san,
        "best_san": best_san,
        "phase": phase,
        "reason": reason_map[cat],
    }


def accuracy_from_losses(losses: List[int]) -> float:
    if not losses:
        return 0.0
    # Stable, monotonic, chess-review-like. Lower ACPL => higher accuracy.
    acpl = sum(losses) / len(losses)
    acc = 103.0 - (0.32 * acpl)
    return round(max(0.0, min(99.9, acc)), 1)


def rating_from_accuracy(acc: float) -> int:
    # Approximate game rating display, not actual Elo.
    return int(max(100, min(2900, round(350 + acc * 25))))


def analyze_pgn_server(pgn_text: str, depth: int) -> Dict[str, Any]:
    engine_path = find_stockfish()
    if not engine_path:
        raise RuntimeError("Stockfish не найден. Укажи путь в переменной STOCKFISH_PATH или установи stockfish.")

    game = parse_game(pgn_text)
    headers = {k: str(v) for k, v in game.headers.items()}
    board = game.board()
    moves = list(game.mainline_moves())
    if not moves:
        raise ValueError("В PGN нет ходов.")

    engine = chess.engine.SimpleEngine.popen_uci(engine_path)
    try:
        # Critical for repeatability.
        try:
            engine.configure({"Threads": 1, "Hash": 128, "Skill Level": 20})
        except Exception:
            pass

        limit = chess.engine.Limit(depth=depth)
        positions = []
        analysis_infos = []
        boards_before = []

        b = game.board()
        positions.append(b.fen())
        analysis_infos.append(engine.analyse(b, limit, multipv=1)[0])

        for mv in moves:
            boards_before.append(b.copy(stack=False))
            b.push(mv)
            positions.append(b.fen())
            analysis_infos.append(engine.analyse(b, limit, multipv=1)[0])

        move_rows = []
        white_losses, black_losses = [], []
        phase_counts = {
            "white": {"opening": 0, "middlegame": 0, "endgame": 0},
            "black": {"opening": 0, "middlegame": 0, "endgame": 0},
        }

        evals = []
        for idx, fen in enumerate(positions):
            sw = score_white(analysis_infos[idx])
            evals.append({"ply": idx, "fen": fen, "score_white_cp": sw, "text": score_text(sw)})

        for i, mv in enumerate(moves):
            before = boards_before[i]
            info_before = analysis_infos[i]
            info_after = analysis_infos[i + 1]
            best_move = info_before.get("pv", [None])[0] if info_before.get("pv") else None
            s_before = score_white(info_before)
            s_after = score_white(info_after)
            cls = classify_move(before, mv, best_move, s_before, s_after, i)

            color_name = "white" if before.turn == chess.WHITE else "black"
            if before.turn == chess.WHITE:
                white_losses.append(cls["loss_cp"])
            else:
                black_losses.append(cls["loss_cp"])
            phase_counts[color_name][cls["phase"]] += 1

            pv = info_before.get("pv", [])
            pv_san = pv_to_san(before, pv)

            move_rows.append({
                "ply": i + 1,
                "move_number": before.fullmove_number,
                "color": color_name,
                "piece": before.piece_at(mv.from_square).symbol() if before.piece_at(mv.from_square) else "",
                **cls,
                "fen_after": positions[i + 1],
                "score_before_white_cp": s_before,
                "score_after_white_cp": s_after,
                "score_after_text": score_text(s_after),
                "pv_san": pv_san,
            })

        def count_categories(color: str) -> Dict[str, int]:
            cats = ["book", "brilliant", "great", "best", "excellent", "good", "inaccuracy", "mistake", "miss", "blunder"]
            return {c: sum(1 for r in move_rows if r["color"] == color and r["category"] == c) for c in cats}

        white_acc = accuracy_from_losses(white_losses)
        black_acc = accuracy_from_losses(black_losses)

        engine_id = "Stockfish"
        try:
            engine_id = str(engine.id.get("name") or "Stockfish")
        except Exception:
            pass

        return {
            "ok": True,
            "engine": engine_id,
            "engine_path": str(engine_path),
            "depth": depth,
            "headers": headers,
            "summary": {
                "white": {
                    "name": headers.get("White", "White"),
                    "accuracy": white_acc,
                    "game_rating": rating_from_accuracy(white_acc),
                    "counts": count_categories("white"),
                    "phases": phase_counts["white"],
                    "acpl": round(sum(white_losses) / len(white_losses), 1) if white_losses else 0,
                },
                "black": {
                    "name": headers.get("Black", "Black"),
                    "accuracy": black_acc,
                    "game_rating": rating_from_accuracy(black_acc),
                    "counts": count_categories("black"),
                    "phases": phase_counts["black"],
                    "acpl": round(sum(black_losses) / len(black_losses), 1) if black_losses else 0,
                },
            },
            "moves": move_rows,
            "evals": evals,
            "created_at": int(time.time()),
        }
    finally:
        try:
            engine.quit()
        except Exception:
            pass


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    engine = find_stockfish()
    return jsonify({"ok": bool(engine), "stockfish_path": engine or ""})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(force=True, silent=True) or {}
    pgn = str(data.get("pgn", "")).strip()
    depth = int(data.get("depth", 16))
    depth = max(8, min(depth, 22))
    if not pgn:
        return jsonify({"ok": False, "error": "PGN пустой."}), 400

    key = hashlib.sha256((pgn + f"|depth={depth}|v=server-v1").encode("utf-8")).hexdigest()
    cache_file = CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        return jsonify(json.loads(cache_file.read_text(encoding="utf-8")))

    try:
        result = analyze_pgn_server(pgn, depth)
        result["cache_key"] = key
        cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    print("Open: http://127.0.0.1:%s" % port)
    print("Stockfish:", find_stockfish() or "NOT FOUND")
    app.run(host=host, port=port, debug=False)
