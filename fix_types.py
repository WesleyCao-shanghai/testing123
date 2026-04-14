#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修复 questions.json 里的题目类型
规则：
  - type=multiple  且答案只有 1 个字母  → 改为 single
  - type=single    且答案有 2+ 个字母   → 改为 multiple
  - type=truefalse 保持不变
手动填写的答案不会受影响。
"""
import json
from pathlib import Path

path = Path("questions.json")
data = json.loads(path.read_text(encoding="utf-8"))

fixed = 0
for q in data["questions"]:
    ans  = q.get("answer", "")
    typ  = q.get("type", "")

    if typ == "truefalse":
        continue

    # 只含字母的有效答案
    letters = [c for c in ans.upper() if c in "ABCD"]

    if not letters:
        continue

    if typ == "multiple" and len(letters) == 1:
        q["type"] = "single"
        fixed += 1
    elif typ == "single" and len(letters) > 1:
        q["type"] = "multiple"
        fixed += 1

path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"✅ 修复完成，共修正 {fixed} 道题的类型。")
