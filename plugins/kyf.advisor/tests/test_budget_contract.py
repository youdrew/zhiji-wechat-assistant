"""Synthetic cross-language budget contract; no model or personal data.

Copyright 2026 Know Your Friends contributors. Apache-2.0.
"""
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import unittest

from contracts import formal_input_budget, managed_context_cost, model_skill_text


class BudgetContractTests(unittest.TestCase):
    def test_python_preparation_and_node_preflight_use_identical_units(self):
        root = Path(__file__).resolve().parents[3]
        node = os.environ.get("KYF_NODE") or shutil.which("node")
        if not node or not (root / "node_modules/tsx").exists():
            self.skipTest("Project Node and tsx are needed for the runtime budget contract")
        rng = random.Random(20261016)
        texts = ["", "a", "中", "😀", "\r\n\r\n", "a\rb\r\nc\nd",
                 "a\u2028b\u2029c", "😀" * 700, "😀" * 701, "中" * 1401,
                 "a\n" * 12000]
        texts += ["".join(rng.choices("abc中😀\n\r\t\u2028\u2029", k=size))
                  for size in (30, 699, 700, 701, 2001, 12000)]
        skills = [[], [{"id": 'synthetic-"\\中😀', "content": "说明\n资料"}],
                  [{"id": "first", "content": "a\u2028b"},
                   {"id": "second", "content": "English and 中文😀"}]]
        budgets = [[window, hidden] for window in (4096, 8192, 32768, 131072, 1000000)
                   for hidden in (0, 1024, 1536, 4096)]
        script = """
import { advisorInputBudget, advisorManagedContextCost, advisorSkillText }
  from './plugins/kyf.advisor/engine/runtime.ts';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const data = JSON.parse(input);
process.stdout.write(JSON.stringify({
  costs: data.texts.map(advisorManagedContextCost),
  skills: data.skills.map(advisorSkillText),
  budgets: data.budgets.map(([window, hidden]) => advisorInputBudget(window, hidden))
}));
"""
        result = subprocess.run([node, "--import", "tsx", "--input-type=module", "-e", script],
                                cwd=root, input=json.dumps({"texts": texts, "skills": skills,
                                                          "budgets": budgets}),
                                text=True, encoding="utf-8", capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)
        self.assertEqual(actual["costs"], [managed_context_cost(text) for text in texts])
        self.assertEqual(actual["skills"], [model_skill_text(items) for items in skills])
        self.assertEqual(actual["budgets"], [formal_input_budget(window, hidden)
                                             for window, hidden in budgets])


if __name__ == "__main__":
    unittest.main()
