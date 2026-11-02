---
theme: mokkapps
title: Uwazi Admin Agent — a web UI for Uwazi admin work
info: |
  A web UI for reviewed, reversible Uwazi administration, with a real
  title-extraction example.
class: dark
colorSchema: dark
highlighter: shiki
transition: slide-left
mdc: true
drawings: false
---

# Uwazi Admin Agent

## A web UI for bulk Uwazi administration

<div class="mt-6 text-sm opacity-70">
Write a task. Review the plan. Run it. Revert it.
</div>

<img src="./images/app-task-list.png" class="hero-shot" />

---
layout: default
---

# A web UI for Uwazi admin work

<div class="grid-3">
  <div class="card">
    <div class="card-kicker">1</div>
    <h3>Connect</h3>
  </div>
  <div class="card">
    <div class="card-kicker">2</div>
    <h3>Describe</h3>
  </div>
  <div class="card">
    <div class="card-kicker">3</div>
    <h3>Review &amp; run</h3>
  </div>
</div>

<img src="./images/app-reverted.png" class="shot contain" />

---
layout: default
---

# How it works

<div class="pipeline">
  <div class="pipe-step">
    <div class="pipe-icon">✍️</div>
    <div class="pipe-title">Prompt</div>
  </div>
  <div class="pipe-arrow">→</div>
  <div class="pipe-step">
    <div class="pipe-icon">🧠</div>
    <div class="pipe-title">Plan</div>
  </div>
  <div class="pipe-arrow">→</div>
  <div class="pipe-step">
    <div class="pipe-icon">🔍</div>
    <div class="pipe-title">Review</div>
  </div>
  <div class="pipe-arrow">→</div>
  <div class="pipe-step">
    <div class="pipe-icon">✅</div>
    <div class="pipe-title">Run / Revert</div>
  </div>
</div>

<div class="callout mt-8">
The LLM proposes. You approve.
</div>


---
layout: default
---

# What it can do

<div class="feature-grid tall">

<div class="feature"><span>✏️</span><strong>Metadata</strong></div>
<div class="feature"><span>🔀</span><strong>Merge entities</strong></div>
<div class="feature"><span>📎</span><strong>Files</strong></div>
<div class="feature"><span>➕</span><strong>Create entities</strong></div>
<div class="feature"><span>🗑️</span><strong>Delete / publish</strong></div>
<div class="feature"><span>🔗</span><strong>Relationships</strong></div>
<div class="feature"><span>🌍</span><strong>Languages</strong></div>
<div class="feature"><span>📊</span><strong>Table sync</strong></div>

</div>

---
layout: default
---

# AI reads documents

<div class="two-up">
  <figure>
    <img src="./images/uwazi-before.png" />
  </figure>
  <div>

<div class="big-list">

- **Titles**
- **Dates**
- **Summaries**
- **Classifications**

</div>

<div class="callout mt-4">
Page 1 in. A proposed value out.
</div>

  </div>
</div>

---
layout: section
---

# Example

## Title extraction from page 1

---
layout: none
---

<div class="step-shot">
  <div class="step-label"><span>1</span> New task</div>
  <img src="./images/wizard-prompt.png" />
</div>

---
layout: none
---

<div class="step-shot">
  <div class="step-label"><span>2</span> Review table</div>
  <img src="./images/review-table.png" />
</div>

---
layout: none
---

<div class="step-shot">
  <div class="step-label"><span>2</span> Inspect the proposal</div>
  <img src="./images/review-expanded.png" />
</div>

---
layout: none
---

<div class="step-shot">
  <div class="step-label"><span>2</span> AI reads page 1</div>
  <img src="./images/entity-window.png" />
</div>

---
layout: none
---

<div class="step-shot">
  <div class="step-label"><span>3</span> Run</div>
  <img src="./images/run-confirm.png" />
</div>

---
layout: none
---

<div class="step-shot">
  <div class="step-label"><span>3</span> Applied</div>
  <img src="./images/applied-table.png" />
</div>

---
layout: none
---

<div class="step-shot">
  <div class="step-label"><span>3</span> Result in Uwazi</div>
  <img src="./images/uwazi-after.png" />
</div>

---
layout: none
---

<div class="step-shot">
  <div class="step-label"><span>4</span> Revert</div>
  <img src="./images/revert-confirm.png" />
</div>

---
layout: none
---

<div class="step-shot">
  <div class="step-label"><span>4</span> Reverted</div>
  <img src="./images/reverted-table.png" />
</div>

---
layout: none
---

<div class="step-shot">
  <div class="step-label"><span>4</span> Back to the original</div>
  <img src="./images/uwazi-reverted.png" />
</div>

---
layout: outro
---

# Write. Review. Run. Revert.

<div class="mt-6 text-base opacity-75">
A web UI for Uwazi admin work.
</div>
