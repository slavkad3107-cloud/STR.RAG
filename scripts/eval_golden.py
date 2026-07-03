"""Golden-set eval harness СтройПроект (v0.21, «сначала измеритель»).

Делает улучшения качества ИЗМЕРИМЫМИ: фиксируем эталонный набор (замечание →
принятый ответ → файлы-источники) и меряем retrieval и качество ответов до/после
любого изменения (чанкинг, кандидаты, эмбеддер, промпты).

Команды:
  1) Собрать golden-set из ПРИНЯТЫХ ответов проекта (статусы accepted/edited):
       python scripts/eval_golden.py build --project "ИМЯ" [--out golden.jsonl]
  2) Замерить retrieval (recall@k, MRR по файлам-источникам эталона):
       python scripts/eval_golden.py run --project "ИМЯ" --golden golden.jsonl
       [--k 8] [--expansion]   (--expansion = как в бою, с LLM-перефразами)
  3) LLM-judge: сравнить ТЕКУЩИЕ предложенные ответы (answers.json) с эталоном:
       python scripts/eval_golden.py judge --project "ИМЯ" --golden golden.jsonl

Результаты печатаются и дописываются в <данные>/eval_history.jsonl — история
замеров между версиями. Типовой цикл: build (один раз) → run/judge (baseline) →
изменение системы → переиндексация при необходимости → run/judge → сравнить.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows-консоль
except Exception:  # noqa: BLE001
    pass


def _history_append(rec: dict) -> None:
    from pmoos.paths import data_root
    p = data_root() / "eval_history.jsonl"
    rec["ts"] = datetime.now().isoformat(timespec="seconds")
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def cmd_build(args) -> int:
    from pmoos.pipeline.block1_answers import load_answers
    data = load_answers(args.project)
    items = []
    for a in data.get("answers", []):
        if a.get("status") not in ("accepted", "edited"):
            continue
        expected = sorted({s.get("file", "") for s in (a.get("sources") or []) if s.get("file")})
        final = (a.get("user_answer") or a.get("answer") or "").strip()
        if not a.get("remark") or not final:
            continue
        items.append({
            "number": str(a.get("number", "")),
            "remark": a.get("remark", ""),
            "expected_files": expected,
            "accepted_answer": final,
            "category": a.get("category", ""),
        })
    out = Path(args.out)
    with out.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    print(f"[golden] собрано эталонов: {len(items)} → {out}")
    if not items:
        print("[golden] ПУСТО: в answers.json нет принятых/правленых ответов. "
              "Сначала примите ответы в Модуле 4.")
        return 1
    n_src = sum(1 for it in items if it["expected_files"])
    print(f"[golden] с файлами-источниками: {n_src}/{len(items)} "
          f"(recall меряется только по ним)")
    return 0


def _load_golden(path: str) -> list[dict]:
    items = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def cmd_run(args) -> int:
    from pmoos.config import load_config
    from pmoos.retrieval.hybrid import HybridRetriever
    items = _load_golden(args.golden)
    scored = [it for it in items if it.get("expected_files")]
    if not scored:
        print("[eval] в golden-set нет эталонов с файлами-источниками"); return 1
    cfg = load_config()
    retr = HybridRetriever(cfg)
    try:
        hits_per = retr.batch_search(args.project, [it["remark"] for it in scored],
                                     top=args.k, use_expansion=args.expansion)
    finally:
        retr.close()
    hit_at_k, rr_sum, per_item = 0, 0.0, []
    for it, hits in zip(scored, hits_per):
        files = [(h.get("payload") or {}).get("file", "") for h in hits]
        exp = set(it["expected_files"])
        rank = next((i + 1 for i, f in enumerate(files) if f in exp), 0)
        if rank:
            hit_at_k += 1
            rr_sum += 1.0 / rank
        per_item.append({"number": it["number"], "rank": rank})
    n = len(scored)
    recall = hit_at_k / n
    mrr = rr_sum / n
    print(f"[eval] проект: {args.project} | эталонов: {n} | k={args.k} | "
          f"expansion={'on' if args.expansion else 'off'}")
    print(f"[eval] recall@{args.k} = {recall:.3f}   MRR = {mrr:.3f}")
    misses = [pi["number"] for pi in per_item if not pi["rank"]]
    if misses:
        print(f"[eval] промахи ({len(misses)}): №" + ", №".join(misses[:20]))
    from pmoos import __version__
    _history_append({"kind": "retrieval", "project": args.project, "version": __version__,
                     "k": args.k, "expansion": bool(args.expansion), "n": n,
                     "recall_at_k": round(recall, 4), "mrr": round(mrr, 4),
                     "misses": misses})
    return 0


_JUDGE_SYS = (
    "Ты — эксперт-эколог госэкспертизы. Сравни НОВЫЙ ответ на замечание с "
    "ЭТАЛОННЫМ (принятым инженером). Оцени, насколько новый ответ покрывает "
    "суть эталона: те же данные/нормативы/выводы. Верни СТРОГО JSON: "
    '{"score": 0..10, "verdict": "лучше|эквивалентно|хуже", "missing": "чего не хватает"}'
)


def cmd_judge(args) -> int:
    from pmoos.config import load_config
    from pmoos.core.ai_providers import chat_json
    from pmoos.pipeline.block1_answers import load_answers
    items = {it["number"]: it for it in _load_golden(args.golden)}
    data = load_answers(args.project)
    cfg = load_config()
    pairs = []
    for a in data.get("answers", []):
        it = items.get(str(a.get("number")))
        new_ans = (a.get("answer") or "").strip()
        if it and new_ans:
            pairs.append((it, new_ans))
    if not pairs:
        print("[judge] нет пересечения golden-set с текущими ответами answers.json "
              "(сначала прогоните «① Найти ответы» в М4)"); return 1
    scores, results = [], []
    for it, new_ans in pairs:
        msgs = [{"role": "system", "content": _JUDGE_SYS},
                {"role": "user", "content":
                 f"ЗАМЕЧАНИЕ:\n{it['remark']}\n\nЭТАЛОННЫЙ ОТВЕТ:\n"
                 f"{it['accepted_answer'][:2000]}\n\nНОВЫЙ ОТВЕТ:\n{new_ans[:2000]}"}]
        try:
            v = chat_json(cfg, msgs, expect="object", module="module4", role="review",
                          use_cache=False)
            s = float(v.get("score", 0))
        except Exception as e:  # noqa: BLE001
            print(f"[judge] №{it['number']}: ошибка ИИ: {e}")
            continue
        scores.append(s)
        results.append({"number": it["number"], "score": s,
                        "verdict": v.get("verdict", ""), "missing": v.get("missing", "")})
        print(f"[judge] №{it['number']}: {s:.0f}/10 ({v.get('verdict', '')})")
    if not scores:
        return 1
    avg = sum(scores) / len(scores)
    low = [r["number"] for r in results if r["score"] <= 5]
    print(f"[judge] средний балл: {avg:.2f}/10 по {len(scores)} ответам; "
          f"слабые (≤5): {', '.join(low) or '—'}")
    from pmoos import __version__
    _history_append({"kind": "judge", "project": args.project, "version": __version__,
                     "n": len(scores), "avg_score": round(avg, 2), "low": low})
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Golden-set eval harness СтройПроект")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="собрать golden-set из принятых ответов")
    b.add_argument("--project", required=True)
    b.add_argument("--out", default="golden.jsonl")
    r = sub.add_parser("run", help="замерить retrieval (recall@k, MRR)")
    r.add_argument("--project", required=True)
    r.add_argument("--golden", default="golden.jsonl")
    r.add_argument("--k", type=int, default=8)
    r.add_argument("--expansion", action="store_true",
                   help="с LLM-расширением запросов (как в бою); по умолчанию без него")
    j = sub.add_parser("judge", help="LLM-сравнение текущих ответов с эталоном")
    j.add_argument("--project", required=True)
    j.add_argument("--golden", default="golden.jsonl")
    a = ap.parse_args(argv)
    return {"build": cmd_build, "run": cmd_run, "judge": cmd_judge}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
