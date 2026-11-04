// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
'use strict';

for (const button of document.querySelectorAll('[data-copy-target]')) {
  const target = document.getElementById(button.dataset.copyTarget);
  const status = document.getElementById(button.getAttribute('aria-describedby'));
  if (!target || !status) continue;
  button.hidden = false;
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(target.textContent.trim());
      status.textContent = '已复制';
    } catch {
      status.textContent = '请选中上面的文字手动复制。';
    }
  });
}
