#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF 题库解析器
将历年考试 PDF 自动解析，去重计频，生成 questions.json

使用方法:
  python3 parse_pdf.py              # 解析当前目录所有 PDF
  python3 parse_pdf.py 2020.pdf 2021.pdf 2022.pdf
  python3 parse_pdf.py exams/       # 指定目录
  python3 parse_pdf.py --debug      # 同时打印每份 PDF 提取的原始文字
"""

from __future__ import annotations
import sys, re, json, unicodedata
from pathlib import Path
from difflib import SequenceMatcher

# ── 依赖检查 ──────────────────────────────────────────────────────────────────
DEBUG = '--debug' in sys.argv

# ── 依赖检查：优先 PyMuPDF，退而求其次 pdfplumber ──
try:
    import fitz  # PyMuPDF

    def extract_text(path: Path) -> str:
        doc  = fitz.open(str(path))
        pages = []
        for page in doc:
            t = page.get_text("text")
            if t:
                pages.append(t)
        return "\n".join(pages)

except ImportError:
    try:
        import pdfplumber

        def extract_text(path: Path) -> str:
            pages = []
            with pdfplumber.open(str(path)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text(x_tolerance=3, y_tolerance=3)
                    if t:
                        pages.append(t)
            return "\n".join(pages)

    except ImportError:
        sys.exit("请先安装依赖：pip3 install pymupdf")


# ─────────────────────────────────────────────────────────────────────────────
# 2. 文字规范化
# ─────────────────────────────────────────────────────────────────────────────

def to_half(s: str) -> str:
    """全角 → 半角（字母/数字/标点），保留中文"""
    result = []
    for c in s:
        cp = ord(c)
        if 0xFF01 <= cp <= 0xFF5E:
            result.append(chr(cp - 0xFEE0))
        elif cp == 0x3000:
            result.append(' ')
        else:
            result.append(c)
    return ''.join(result)


def normalize_line(line: str) -> str:
    line = to_half(line)
    # 统一常见标点
    line = line.replace('·', '.').replace('•', '.').replace('●', '.')
    line = re.sub(r'\s{2,}', ' ', line)
    return line.strip()


def normalize_for_compare(s: str) -> str:
    """用于去重比较：去空白、统一标点、小写"""
    s = to_half(s)
    s = re.sub(r'[\s\u3000]+', '', s)
    for old, new in [('，', ','), ('。', '.'), ('、', ','), ('；', ';'),
                     ('：', ':'), ('（', '('), ('）', ')'), ('"', '"'),
                     ('"', '"'), ('【', '['), ('】', ']')]:
        s = s.replace(old, new)
    return s.lower()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_for_compare(a),
                           normalize_for_compare(b)).ratio()


# ─────────────────────────────────────────────────────────────────────────────
# 3. 正则模式
# ─────────────────────────────────────────────────────────────────────────────

# 题号（行首）: 1. / 1、/ 1） / （1） / (1) / 第1题
RE_QNUM = re.compile(
    r'^[ \t]*'
    r'(?:'
    r'第\s*(\d+)\s*[题题目]'       # 第1题 / 第 1 题目
    r'|[（(]\s*(\d+)\s*[）)]\s*[.、]?'  # （1） (1)
    r'|(\d+)\s*[.、．）)。]\s'           # 1. 1、 1） （要求后面有空格）
    r')',
    re.MULTILINE
)

# 选项行（行首）: A. A、 A） A:  （支持全角字母已提前转半角）
RE_OPT = re.compile(
    r'^[ \t]*([A-Da-d])\s*[.、．:：）)]\s*(.+)',
    re.MULTILINE
)

# 章节标题（判断题型归属）
RE_SECTION = re.compile(
    r'(单项?选择题|多项?选择题|判断题|单选题|多选题)',
    re.IGNORECASE
)

# 答案标签（题目内嵌）
RE_ANS_LABEL = re.compile(
    r'(?:【?(?:正确|参考)?答案】?|答案\s*[：:])\s*'
    r'([A-Da-d]{1,4}|[√×✓✗对错正确错误]{1,2})',
    re.IGNORECASE
)

# 答案内嵌在括号中（判断：括号里是√×；选择：括号里是字母，且在行尾）
RE_ANS_TF_BRACKET  = re.compile(r'[（(]\s*([√×✓✗对错正确错误])\s*[）)]')
RE_ANS_CHO_BRACKET = re.compile(r'[（(]\s*([A-Da-d]{1,4})\s*[）)]\s*$')

# 答案区标题（独立答案页）
RE_ANSKEY_HEADER = re.compile(
    r'^[ \t]*(?:参考)?答案(?:与解析|一览|汇总)?[ \t]*$'
    r'|^[ \t]*【?答案】?[ \t]*$',
    re.MULTILINE
)

# 答案区逐题格式：  1.B  /  1、AB  /  1.√
RE_ANSKEY_ITEM = re.compile(
    r'(?<!\d)(\d+)\s*[.、．:：]\s*([A-Da-d]{1,4}|[√×✓✗对错正确错误])\b'
)

# 解析/分析块
RE_EXPLN = re.compile(r'^[ \t]*【?(?:解析|分析|解题思路)】?[ \t:：]*', re.MULTILINE)


# ─────────────────────────────────────────────────────────────────────────────
# 4. 答案规范化
# ─────────────────────────────────────────────────────────────────────────────

TF_TRUE  = {'√', '✓', '对', '正确', 'true',  't', '是', '1', 'y', 'yes', 'v'}
TF_FALSE = {'×', '✗', '错', '错误', 'false', 'f', '否', '0', 'n', 'no',  'x'}

def norm_answer(raw: str, qtype: str | None) -> str | None:
    if not raw:
        return None
    r = raw.strip().lower()
    if qtype == 'truefalse' or (qtype is None and r in TF_TRUE | TF_FALSE):
        if r in TF_TRUE:  return 'true'
        if r in TF_FALSE: return 'false'
        return None
    # choice
    letters = re.sub(r'[^A-Da-d]', '', raw).upper()
    letters = ''.join(sorted(set(letters)))
    return letters or None


# ─────────────────────────────────────────────────────────────────────────────
# 5. 题目类
# ─────────────────────────────────────────────────────────────────────────────

class Question:
    def __init__(self, num: int, section_type: str | None):
        self.num         = num
        self.stem        = ''
        self.options: dict[str, str] = {}
        self.answer: str | None = None
        self.type: str | None = section_type
        self.explanation = ''

    def finalize(self):
        """去除答案括号、推断题型"""
        # 清理题干中的内嵌答案标记（已提取）
        self.stem = self.stem.strip()

        if self.type is None:
            if self.options:
                if self.answer and len(self.answer) > 1:
                    self.type = 'multiple'
                else:
                    self.type = 'single'
            else:
                self.type = 'truefalse'
        elif self.type == 'single' and self.answer and len(self.answer) > 1:
            self.type = 'multiple'

    def is_valid(self) -> bool:
        if not self.stem or len(self.stem) < 3:
            return False
        if self.type in ('single', 'multiple') and len(self.options) < 2:
            return False
        return True

    def to_dict(self, qid: int) -> dict:
        return {
            'id':          qid,
            'type':        self.type or 'single',
            'stem':        self.stem,
            'options':     self.options if self.options else None,
            'answer':      self.answer or '',
            'frequency':   1,
            'explanation': self.explanation.strip(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 6. 核心解析
# ─────────────────────────────────────────────────────────────────────────────

def parse_text(raw_text: str) -> list[Question]:
    # 规范化每行
    lines = [normalize_line(l) for l in raw_text.splitlines()]
    text  = '\n'.join(lines)

    # ── 6a. 检测独立答案区 ──
    ans_key_match = RE_ANSKEY_HEADER.search(text)
    body_text = text[:ans_key_match.start()] if ans_key_match else text
    key_text  = text[ans_key_match.start():] if ans_key_match else ''

    standalone: dict[int, str] = {}
    if key_text:
        for m in RE_ANSKEY_ITEM.finditer(key_text):
            n   = int(m.group(1))
            ans = m.group(2)
            standalone[n] = ans

    # ── 6b. 收集章节标题位置 ──
    # 用来给每个题号分配章节类型
    section_positions: list[tuple[int, str]] = []  # (pos, type)
    for m in RE_SECTION.finditer(body_text):
        label = m.group(1)
        if '单' in label:  stype = 'single'
        elif '多' in label: stype = 'multiple'
        else:              stype = 'truefalse'
        section_positions.append((m.start(), stype))

    def get_section_type(pos: int) -> str | None:
        stype = None
        for sp, st in section_positions:
            if sp <= pos:
                stype = st
        return stype

    # ── 6c. 找所有题号位置 ──
    num_positions: list[tuple[int, int, int]] = []  # (match_start, match_end, num)
    for m in RE_QNUM.finditer(body_text):
        num = int(m.group(1) or m.group(2) or m.group(3))
        num_positions.append((m.start(), m.end(), num))

    if not num_positions:
        return []

    # ── 6d. 按题号切块，逐块解析 ──
    questions: list[Question] = []
    for i, (qstart, qend, qnum) in enumerate(num_positions):
        next_start = num_positions[i + 1][0] if i + 1 < len(num_positions) else len(body_text)
        block = body_text[qend:next_start]

        stype = get_section_type(qstart)
        q = Question(qnum, stype)

        # 解析块内各行
        stem_lines  = []
        in_exp      = False

        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue

            # 解析/解题思路 → 后续归入 explanation
            if RE_EXPLN.match(line):
                in_exp = True
                tail = RE_EXPLN.sub('', line).strip()
                if tail:
                    q.explanation += tail + ' '
                continue

            if in_exp:
                # 在解析区，如果碰到答案标签则提取
                am = RE_ANS_LABEL.search(line)
                if am:
                    q.answer = norm_answer(am.group(1), q.type)
                else:
                    q.explanation += line + ' '
                continue

            # 答案标签行
            am = RE_ANS_LABEL.search(line)
            if am and not RE_OPT.match(line):
                q.answer = norm_answer(am.group(1), q.type)
                continue

            # 选项行
            om = RE_OPT.match(line)
            if om:
                key = om.group(1).upper()
                val = om.group(2).strip()
                q.options[key] = val
                continue

            # 题干行（默认）
            if not q.options:  # 还没遇到选项，仍是题干
                stem_lines.append(line)

        q.stem = ' '.join(stem_lines)

        # ── 6e. 从题干括号里提取内嵌答案 ──
        if not q.answer:
            # 判断题：括号里是 √×
            m = RE_ANS_TF_BRACKET.search(q.stem)
            if m:
                q.answer = norm_answer(m.group(1), 'truefalse')
                q.stem   = (q.stem[:m.start()] + q.stem[m.end():]).strip()

        if not q.answer and q.options:
            # 选择题：括号里是字母，且在题干行尾
            m = RE_ANS_CHO_BRACKET.search(q.stem)
            if m:
                q.answer = norm_answer(m.group(1), q.type or 'single')
                q.stem   = q.stem[:m.start()].strip()

        # ── 6f. 从独立答案区补充 ──
        if not q.answer and qnum in standalone:
            q.answer = norm_answer(standalone[qnum], q.type)

        q.finalize()
        if q.is_valid():
            questions.append(q)

    return questions


# ─────────────────────────────────────────────────────────────────────────────
# 7. 跨文件去重 + 频率统计
# ─────────────────────────────────────────────────────────────────────────────

SIMILARITY_THRESHOLD = 0.82  # 相似度高于此视为同一道题

def deduplicate(by_file: list[tuple[str, list[Question]]]) -> list[dict]:
    """
    返回去重后的题目列表，每题含 frequency 字段。
    采用 O(n²) 相似度比较，题量 < 1000 时没问题。
    """
    unique: list[tuple[Question, set]] = []  # (question, set_of_files)

    for filepath, questions in by_file:
        for q in questions:
            best_ratio = 0.0
            best_idx   = -1
            for i, (uq, _) in enumerate(unique):
                r = similarity(q.stem, uq.stem)
                if r > best_ratio:
                    best_ratio = r
                    best_idx   = i

            if best_ratio >= SIMILARITY_THRESHOLD and best_idx >= 0:
                # 合并到已有题目
                uq, files = unique[best_idx]
                files.add(filepath)
                # 补全答案
                if not uq.answer and q.answer:
                    uq.answer = q.answer
                # 补全选项
                if not uq.options and q.options:
                    uq.options = q.options
                # 补全解析
                if not uq.explanation and q.explanation:
                    uq.explanation = q.explanation
            else:
                unique.append((q, {filepath}))

    result = []
    for i, (q, files) in enumerate(unique):
        d = q.to_dict(i + 1)
        d['frequency'] = len(files)
        result.append(d)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 8. 主流程
# ─────────────────────────────────────────────────────────────────────────────

def collect_pdfs(args: list[str]) -> list[Path]:
    args = [a for a in args if not a.startswith('--')]
    paths = []
    if not args:
        paths = sorted(Path('.').glob('*.pdf')) + sorted(Path('.').glob('*.PDF'))
    else:
        for a in args:
            p = Path(a)
            if p.is_dir():
                paths += sorted(p.glob('*.pdf')) + sorted(p.glob('*.PDF'))
            elif p.suffix.lower() == '.pdf' and p.exists():
                paths.append(p)
            else:
                print(f"⚠  跳过（文件不存在）: {a}")
    return paths


def main():
    pdf_paths = collect_pdfs(sys.argv[1:])

    if not pdf_paths:
        print("❌ 未找到 PDF 文件。")
        print("   用法: python3 parse_pdf.py           # 当前目录")
        print("         python3 parse_pdf.py exam/      # 指定目录")
        print("         python3 parse_pdf.py a.pdf b.pdf  # 指定文件")
        sys.exit(1)

    print(f"\n共找到 {len(pdf_paths)} 份 PDF：")
    for p in pdf_paths:
        print(f"  · {p.name}")
    print()

    by_file: list[tuple[str, list[Question]]] = []
    total_raw = 0

    for pdf in pdf_paths:
        print(f"正在解析: {pdf.name} ...", end='', flush=True)
        try:
            raw = extract_text(pdf)
            if DEBUG:
                print(f"\n{'='*60}\n{pdf.name} 原始文字:\n{'='*60}")
                print(raw[:3000])
                print("... (截断)")
                print('='*60)
            qs = parse_text(raw)
            by_file.append((str(pdf), qs))
            total_raw += len(qs)
            print(f" → 提取 {len(qs)} 题")
        except Exception as e:
            print(f" ❌ 失败: {e}")
            if DEBUG:
                import traceback; traceback.print_exc()

    if total_raw == 0:
        print("\n❌ 所有 PDF 均未提取到题目。")
        print("   提示：运行 python3 parse_pdf.py --debug 查看原始提取文字，")
        print("   然后反馈给我，我来调整解析规则。")
        sys.exit(1)

    print(f"\n共提取 {total_raw} 题（含重复），去重中...", end='', flush=True)
    final = deduplicate(by_file)
    final.sort(key=lambda q: (-q['frequency'], q['id']))
    for i, q in enumerate(final):
        q['id'] = i + 1
    print(f" 完成，共 {len(final)} 道独立题目。")

    # 高频阈值：出现在 ≥35% 的卷子中
    n_exams   = len(pdf_paths)
    threshold = max(2, round(n_exams * 0.35))

    output = {
        "meta": {
            "total_exams":        n_exams,
            "high_freq_threshold": threshold,
            "note": f"frequency >= {threshold} 为高频（共 {n_exams} 份卷子）"
        },
        "questions": final
    }

    out = Path('questions.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # ── 统计报告 ──
    hf    = sum(1 for q in final if q['frequency'] >= threshold)
    no_ans = sum(1 for q in final if not q['answer'])
    by_type = {}
    for q in final:
        by_type[q['type']] = by_type.get(q['type'], 0) + 1

    print(f"""
╔══════════════════════════════════════════════╗
║  解析完成                                     ║
╠══════════════════════════════════════════════╣
║  独立题目总数   : {len(final):<5}                       ║
║  高频题 (≥{threshold}次): {hf:<5}                       ║
║  单选题         : {by_type.get('single',0):<5}                       ║
║  多选题         : {by_type.get('multiple',0):<5}                       ║
║  判断题         : {by_type.get('truefalse',0):<5}                       ║
║  ⚠ 答案未识别  : {no_ans:<5}（需手动补充）            ║
╚══════════════════════════════════════════════╝
""")

    print(f"✅ 已写入: {out.absolute()}")
    print()

    if no_ans:
        print(f"⚠  有 {no_ans} 道题未能识别答案（answer 字段为空）。")
        print("   请打开 questions.json，搜索 \"answer\": \"\"，手动填入正确答案。")
        print()
    print("下一步：用浏览器打开刷题网页：")
    print("   python3 -m http.server 8080")
    print("   然后访问 http://localhost:8080/quiz.html")


if __name__ == '__main__':
    main()
