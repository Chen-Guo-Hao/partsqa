"""依實際清理結果產生「資料清理說明報告」（Markdown）。

數字全部來自 run_cleaning() 回傳的統計，不手動填寫。
"""

from __future__ import annotations

from pathlib import Path

from .config import PROJECT_ROOT
from .domain import get_domain

_D = get_domain()
_DESC = {
    "parts_master": "料號主檔（FG 成品／SA 半成品／RM 原材料）",
    "part_attributes": "材料規格屬性（直式，一列一個屬性）",
    "bom": "多階 BOM（含版次、生效／失效日、用量單位、損耗率）",
    "alternates": "替代料主檔",
    "inventory": f"各廠批號層級庫存（快照日 {_D.baseline_date.isoformat()}）",
    "open_po": "採購單明細（含已結案、已取消）",
}
_TBL = {"parts_master": "parts", "part_attributes": "attributes", "bom": "bom",
        "alternates": "alternates", "inventory": "inventory", "open_po": "open_po"}
# 以實際檔名查表（檔名定義於 config/domain.json）
_FILE_DESC = {_D.files[k]: v for k, v in _DESC.items()}
_TABLE = {_D.files[k]: v for k, v in _TBL.items()}
_LINE_RULE = "、".join(f"`FG{k}…`={v['code']}" for k, v in _D.product_lines.items())

_RULES = f"""## 4. 處理規則與理由（摘要）

| 規則 | 處理方式 | 理由（資料字典章節） |
|---|---|---|
| 料號標準化 | 去頭尾空白、全形轉半形（NFKC）、轉大寫、`–—－−` 轉 `-`；仍不符格式者為無效料號，不猜測修正（不把 O 當 0） | 第 3 節 |
| 孤兒資料 | 其他檔案出現但主檔不存在的料號 → 該列排除 | 第 3 節第 4 點 |
| 產品線 | 一律以成品料號判定（{_LINE_RULE}），主檔 ProductLine 不一致時以料號為準 | 第 3 節第 5 點 |
| 允許值欄位 | 先去頭尾空白；Lifecycle／ApprovalStatus／Status 不分大小寫；其他（QCStatus、UoM、Y/N）大小寫須相符；清單外寫法（OK、PASS、已驗、量產、停產、核准中、Hold、部分到貨…）一律無效，不自行對應 | 第 11 節 |
| 日期 | 只接受 `YYYY-MM-DD`、`YYYY/MM/DD`、`YYYYMMDD`；`MM/DD/YYYY`、`01-Jan-2024` 等即使可推測也視為無效 | 第 10 節第 4 點 |
| 數值 | 嚴格解析，不接受「約500」「8週」「TBD」「x2」；不推估、不補值 | 第 11 節 |
| 屬性換算 | 依第 6 節換算為標準單位（µF、Ω、µH、V、A、W、%、°C、月），支援 p/n/µ/μ/u/m/k/K/M 字首、ohm、℃、Unicode 減號；電阻只寫 k/K/M 視為 kΩ/MΩ；只有數字視為標準單位；無法換算視為缺值 | 第 6 節 |
| 屬性重複／衝突 | 同料號同屬性換算後相同 → 重複保留一筆；不同 → 待人工確認，確認前該屬性視為缺值 | 第 6 節 |
| BOM 損耗率 | 空白=0；帶 % 換算為小數；無 % 一律視為小數；超出 0～0.2 或無法判讀 → 以 0 計算並列入報告（唯一允許的預設值） | 第 8 節第 4 點 |
| BOM 無效行 | 用量非正數、日期無效、失效日早於生效日、料號無效或不存在、單位無法換算為下階主檔單位 → 無效行 | 第 8 節第 2、5 點 |
| BOM 衝突／循環 | 同上階＋版次＋行號內容不同、或形成循環參照（以 Tarjan 強連通元件偵測）→ 待人工確認，確認前不展開 | 第 8 節第 6、7 點 |
| 替代料無效紀錄 | 主料＝替代料、用量比無效、日期無效或顛倒、範圍值無效、核准狀態不在清單 → 無效 | 第 9 節 A11 |
| 替代料衝突／Pending | 同主料＋替代料＋範圍＋範圍值多筆不同 → 待人工確認；Pending → 須主料類別的 R4 核准；Rejected 不可用 | 第 9 節 A1、A11 |
| 替代料備註 | Remark 僅供參考、不具核准效力；含「給 AI 的指令」的備註只標記、不執行 | 第 4.4 節 |
| 庫存 | 工廠別名對應標準代碼、無效工廠排除；QCStatus 只有「合格」可用；在庫量負數／非數字、保留量大於在庫量 → 無效批次；KPCS×1000；單位須與主檔一致；DateCode 無效者記錄下來，於有 ShelfLifeMonths 的料號計算時不可用 | 第 7、10 節 |
| 在途 | 只計 Status=Open 的未交量；ETA 無效不計；ETA 早於下單日為日期矛盾一律排除；ETA+3 天 ≤ 生產日（於問答計算時依生產日判斷） | 第 10 節第 3 點 |
| 主檔衝突 | 同料號多筆內容不同 → 待人工確認；確認前其他檔案的相關列暫不使用（問答時動態判斷，R4 核准後立即生效，不需重跑清理） | 第 11 節 |
| 重複列 | 原始列完全相同 → 保留一筆；標準化後內容相同 → 亦保留一筆 | 第 11 節 |
"""

_NOT_HANDLED = """## 6. 無法或刻意不處理的項目

| 項目 | 原因 |
|---|---|
| 無效料號（字母 O／數字 0、字母 I／數字 1 混用等） | 規則禁止猜測修正（O/0、I/1 混用），只排除並列入報告 |
| `MM/DD/YYYY`、`01-Jan-2024` 等日期 | 規則規定即使可推測也視為無效，故該 BOM 行不展開 |
| `pcs`、`Pcs`、`ea` 等大小寫不符的主檔 UoM | UoM 欄位未註明「不分大小寫」，依第 11 節只接受清單值；該料號單位視為缺值，相關 BOM 行與庫存因無法換算而不使用 |
| QCStatus `OK`／`PASS`／`已驗`、Lifecycle `量產`／`停產`、ApprovalStatus `核准中`／`OK` | 清單外寫法一律無效，不自行對應 |
| 待人工確認資料 | 系統無法自動判定，需由負責該物料類別的 R4 於 UI 核准／駁回後才生效（核准紀錄保留在 app DB） |
| 效期（ShelfLifeMonths） | 與生產日相關，於問答時依生產日動態判斷，清理階段只解析 DateCode |
| 在途是否來得及 | 與生產日相關，於問答時判斷 `ETA + 3 天 ≤ 生產日` |
| 替代料備註中的指令文字 | 視為資料，不執行（見 REMARK_INJECTION） |
"""


def _fmt_examples(ex: list) -> str:
    return "、".join(f"`{e}`" for e in ex[:3]).replace("|", "/") if ex else ""


def write_cleaning_report(stats: dict, report_dir: Path, out_path: Path | None = None) -> Path:
    """產生完整報告：專案根目錄的 資料清理說明報告.md（並在 report_dir 留一份副本）。"""
    lines = [
        f"# 資料清理說明報告（{_D.system_name}）",
        "",
        f"> 本報告由 `scripts/run_clean.py` 依實際執行結果自動產生（執行時間 {stats['run_at']}，耗時 {stats['elapsed_s']} 秒）。",
        "> 清理規則依 `data/data_dictionary.md`；原始 CSV 唯讀，清理結果寫入 SQLite（`PQA_CLEAN_DB`）。",
        "",
        "## 1. 資料來源與檔案清單",
        "",
        "| 檔案 | 內容 | 原始筆數 | 欄位 |",
        "|---|---|---|---|",
    ]
    for f, c in stats["counts"].items():
        lines.append(f"| {f} | {_FILE_DESC[f]} | {c['raw_rows']:,} | {', '.join(c['raw_columns'])} |")
    lines += [
        "",
        f"編碼 UTF-8（無 BOM）、逗號分隔、第一列為欄位名稱；資料基準日 {_D.baseline_date.isoformat()}。",
        "",
        "## 2. 清理前後筆數對照",
        "",
        "「儲存」= 去除重複後寫入資料庫的筆數；每列帶狀態：可用（ok）／無效（invalid，保留以便問答時說明排除筆數）／待人工確認（pending）。",
        "",
        "| 檔案 | 資料表 | 原始 | 儲存 | 可用 | 無效 | 待人工確認 |",
        "|---|---|---|---|---|---|---|",
    ]
    for f, c in stats["counts"].items():
        lines.append(f"| {f} | `{_TABLE[f]}` | {c['raw_rows']:,} | {c['stored_rows']:,} | {c['ok']:,} | {c['invalid']:,} | {c['pending']:,} |")
    lines += ["", "### 2.1 清理後欄位", ""]
    for f, c in stats["counts"].items():
        lines.append(f"- `{_TABLE[f]}`：{', '.join(c['clean_columns'])}")
    lines += ["", "## 3. 發現的資料問題（實際數量與範例）", ""]
    by_file: dict[str, list] = {}
    for it in stats["issues"]:
        by_file.setdefault(it["file"], []).append(it)
    for f, items in by_file.items():
        lines += [f"### {f}", "", "| 代碼 | 問題 | 筆數 | 處理方式 | 範例 |", "|---|---|---|---|---|"]
        for it in items:
            lines.append(f"| {it['code']} | {it['desc']} | {it['count']:,} | {it['handling']} | {_fmt_examples(it['examples'])} |")
        lines.append("")
    lines.append(_RULES)
    lines += ["## 5. 待人工確認清單", "", f"共 {stats['review_items']:,} 筆，依類型：", "",
              "| 類型 | 筆數 | 說明 |", "|---|---|---|"]
    desc = {"ALT_PENDING": "Pending 替代料，須主料類別 R4 核准", "PART_MASTER_CONFLICT": "主檔同料號多筆內容不同（每筆候選各一項）",
            "ATTRIBUTE_CONFLICT": "同料號同屬性換算後值不同（每個候選值一項）", "BOM_LINE_CONFLICT": "同上階版次行號內容不同",
            "BOM_CYCLE": "BOM 循環參照", "ALT_RECORD_CONFLICT": "替代料同鍵多筆內容不同"}
    for k, v in stats["review_by_type"].items():
        lines.append(f"| {k} | {v:,} | {desc.get(k, '')} |")
    lines += ["", "R4 零件工程師登入後可在「人工審核」頁逐筆核准／駁回（只看得到自己負責類別的項目），核准紀錄寫入 app DB 的 `review_decisions` 表，重跑清理不會遺失。", ""]
    lines.append(_NOT_HANDLED)
    lines += [
        "## 7. 如何重現清理流程",
        "",
        "```bash",
        "# 1) 設定 .env 中的 PQA_DATA_DIR 指向六個原始 CSV 所在資料夾",
        "# 2) 以命令列執行（或由 R5 系統管理員在 UI「資料清理」頁按下執行）",
        "python -m scripts.run_clean --user r5_admin",
        "```",
        "",
        "- 程式：`partsqa/cleaning.py`（規則）、`partsqa/normalize.py`、`partsqa/attributes.py`、`partsqa/units.py`",
        "- 輸出：`data/processed/partsqa_clean.db`、`data/processed/clean_stats.json`、本報告",
        "- 可重複執行：每次從原始 CSV 重建（先寫入暫存檔再原子替換），原始資料不被修改。",
        "",
    ]
    text = "\n".join(lines)
    out = out_path or (PROJECT_ROOT / "資料清理說明報告.md")
    out.write_text(text, encoding="utf-8")
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "clean_report.md").write_text(text, encoding="utf-8")
    return out
