import math
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

import brain_util as bu


@dataclass(frozen=True, slots=True)
class ChessConfig:
    region: str = "NONE"
    scale: float = 1.0
    chess_agent: str = "chess"
    parser_agent: str = "parser"
    grid_size: int = 8
    grid_color: str = "rgba(0,255,200,0.95)"
    grid_stroke_width: int = 4
    arrow_color: str = "rgba(255,60,60,0.9)"
    arrow_stroke_width: int = 8
    chess_max_tokens: int = 200
    parser_max_tokens: int = 30
    post_move_delay: float = 5.0
    failure_delay: float = 1.0


CHESS_SYSTEM: str = """\
You are a chess vision model analyzing a board screenshot.
White pieces are at the bottom of the board.
A green grid overlay marks columns a-h (left to right) and rows 1-8 (bottom to top).
{previous_move_note}

Analyze the position step by step.
Identify all White and Black pieces and their squares.
Then choose the single best legal move for White.
State the move clearly at the end of your response.\
"""

CHESS_USER: str = """\
Look at this chess board screenshot carefully.
Identify all pieces and their positions using the green grid.
What is the best move for White? Explain your reasoning, then state the move.\
"""

PARSER_SYSTEM: str = """\
You are a text extraction utility.
The user will give you chess analysis text.
Extract the chess move and convert it to UCI notation.
UCI notation is exactly 4 or 5 lowercase characters: source square + destination square + optional promotion piece.
Examples: e2e4, g1f3, e7e8q, e1g1
Respond with ONLY the UCI move. Nothing else.\
"""

PARSER_USER: str = """\
Extract the move from this chess analysis and convert it to UCI notation.
Respond with only the UCI move, nothing else.

{raw_text}\
"""


def _uci_to_norm(square: str, grid_size: int = 8) -> tuple[int, int]:
    col = ord(square[0]) - ord('a')
    row = int(square[1]) - 1
    step = bu.SHARED.norm // grid_size
    x = col * step + step // 2
    y = bu.SHARED.norm - (row * step + step // 2)
    return x, y


def _parse_uci(text: str) -> str:
    clean = re.sub(r'<think>.*?</think>', ' ', text, flags=re.DOTALL)
    clean = clean.strip().lower()
    for token in clean.replace('\n', ' ').split():
        stripped = token.strip('.,;:!?()[]{}"\' ')
        stripped = stripped.replace('=', '')
        if len(stripped) in (4, 5):
            if stripped[0] in 'abcdefgh' and stripped[1] in '12345678':
                if stripped[2] in 'abcdefgh' and stripped[3] in '12345678':
                    if len(stripped) == 5 and stripped[4] not in 'qrbn':
                        continue
                    return stripped
    return ""


def _make_grid_overlays(grid_size: int, color: str, stroke_width: int) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []
    step = bu.SHARED.norm // grid_size
    for i in range(grid_size + 1):
        pos = i * step
        overlays.append(bu.overlay(
            points=[[pos, 0], [pos, bu.SHARED.norm]], stroke=color, stroke_width=stroke_width))
        overlays.append(bu.overlay(
            points=[[0, pos], [bu.SHARED.norm, pos]], stroke=color, stroke_width=stroke_width))
    return overlays


def _make_arrow_overlay(
    from_sq: str, to_sq: str,
    color: str, grid_size: int, stroke_width: int = 8,
    label: str = "",
) -> list[dict[str, Any]]:
    step = bu.SHARED.norm // grid_size
    fx, fy = _uci_to_norm(from_sq, grid_size)
    tx, ty = _uci_to_norm(to_sq, grid_size)
    dx, dy = tx - fx, ty - fy
    length = math.hypot(dx, dy)
    if length == 0:
        return []
    ux, uy = dx / length, dy / length
    head_len = step * 0.55
    head_width = step * 0.32
    shaft_tip_x = tx - ux * head_len
    shaft_tip_y = ty - uy * head_len
    px, py = -uy, ux
    w1x = round(shaft_tip_x + px * head_width)
    w1y = round(shaft_tip_y + py * head_width)
    w2x = round(shaft_tip_x - px * head_width)
    w2y = round(shaft_tip_y - py * head_width)
    return [
        bu.overlay(
            points=[[round(fx), round(fy)], [round(shaft_tip_x), round(shaft_tip_y)]],
            stroke=color, stroke_width=stroke_width),
        bu.overlay(
            points=[[round(tx), round(ty)], [w1x, w1y], [w2x, w2y]],
            closed=True, fill=color, stroke=color, stroke_width=1, label=label),
    ]


def _run_round(
    cfg: ChessConfig,
    grid_overlays: list[dict[str, Any]],
    previous_uci: str,
) -> str:
    base_b64 = bu.capture(cfg.chess_agent, cfg.region, scale=cfg.scale)
    if not base_b64:
        return ""

    overlays = list(grid_overlays)
    previous_move_note = ""
    if previous_uci:
        from_sq = previous_uci[:2]
        to_sq = previous_uci[2:4]
        overlays.extend(_make_arrow_overlay(
            from_sq, to_sq, cfg.arrow_color, cfg.grid_size,
            cfg.arrow_stroke_width, label=previous_uci))
        previous_move_note = f"A red arrow on the board shows the previous move: {previous_uci}. Do NOT repeat this move."

    annotated_b64 = bu.annotate(cfg.chess_agent, base_b64, overlays)
    if not annotated_b64:
        annotated_b64 = base_b64

    system_prompt = CHESS_SYSTEM.format(previous_move_note=previous_move_note)
    chess_reply = bu.vlm_text(
        cfg.chess_agent,
        bu.make_vlm_request(
            system_prompt, CHESS_USER,
            image_b64=annotated_b64,
            max_tokens=cfg.chess_max_tokens,
        ),
    )
    if not chess_reply:
        return ""

    parser_user = PARSER_USER.format(raw_text=chess_reply)
    parser_reply = bu.vlm_text(
        cfg.parser_agent,
        bu.make_vlm_request(
            PARSER_SYSTEM, parser_user,
            max_tokens=cfg.parser_max_tokens,
        ),
    )
    if not parser_reply:
        return ""

    uci_move = _parse_uci(parser_reply)
    if not uci_move:
        uci_move = _parse_uci(chess_reply)
    if not uci_move:
        return ""

    if uci_move == previous_uci:
        return ""

    from_sq = uci_move[:2]
    to_sq = uci_move[2:4]
    from_x, from_y = _uci_to_norm(from_sq, cfg.grid_size)
    to_x, to_y = _uci_to_norm(to_sq, cfg.grid_size)

    bu.device(cfg.chess_agent, cfg.region, [
        {"type": "drag", "x1": from_x, "y1": from_y, "x2": to_x, "y2": to_y}
    ])

    return uci_move


def main() -> None:
    args = bu.parse_brain_args(sys.argv[1:])
    cfg = ChessConfig(region=args.region, scale=args.scale)
    grid_overlays = _make_grid_overlays(cfg.grid_size, cfg.grid_color, cfg.grid_stroke_width)
    previous_uci: str = ""

    while True:
        try:
            result = _run_round(cfg, grid_overlays, previous_uci)
        except Exception:
            result = ""

        if result:
            previous_uci = result
            time.sleep(cfg.post_move_delay)
        else:
            time.sleep(cfg.failure_delay)


if __name__ == "__main__":
    main()
