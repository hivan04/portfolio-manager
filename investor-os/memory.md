# Memory — Ivan

Personal context, evolving views, and timestamped changes. Append-only below the dividers.

---

## Current state (as of 2026-08-08)

### Identity
- **Name:** Ivan
- **Age:** 22
- **Operating base:** London, UK — relocating to Hong Kong ~September 2026
- **Citizenship:** US / British / Hong Kong (triple).
- **Family:** No dependents. Receiving family financial support and rent-free family housing in Hong Kong.

> **Out of scope for this system:** tax, capital gains, and cross-border tax treatment are deliberately excluded from these files. Do not raise them, model them, or factor them into advice. Ivan handles that separately with a professional.

### Work
- Wealth management / trading. Currently an intern.
- **Internship ends end of August 2026.** No role secured after that.
- **Income:** £4,000/mo gross, **£3,711/mo net (confirmed 2026-08-08)**. £1,000 bonus received at start of internship. Internship duration 10 weeks.
- **From September 2026: income is zero.** Family support and savings only until a new role is secured.
- Time available for portfolio: currently constrained — cannot look at markets during work hours. **This constraint disappears in September and is the single largest behavioural risk in the profile.**

### Portfolio posture
- **Capital base:** £50–100k total, of which **£44,051 is the Trading 212 account (audited 2026-08-08)** and the balance sits in a separate high-yield savings account plus the CFD/options trading account — neither visible in the portfolio feed. ~£50k received from family, remainder earned. Fully under Ivan's own discretion — no family veto. **TODO: reconcile the savings and trading account balances so total capital is a measured number rather than a band.**
- **Stated purpose (resolved 2026-08-08):** Exit first, retirement underneath. The capital buys the ability to stop working in a bank and run his own businesses from Asia; the generational book funds retirement regardless of whether the businesses work. **Preservation trigger is children — not an age, not a number.**
- **Allocation — AUDITED 2026-08-08 against the live Trading 212 account.** Prior figures were recalled estimates and were materially wrong. Source: `data/portfolio.json`, synced 2026-08-08.
  - **74 positions**, not 57.
  - **Account value £44,051.30.** Cost basis £37,877.99. Unrealised **+£6,116.37 (+16.15%)**. Realised all-time **+£1,251.59**. The book is performing well — that is not the problem.
  - **Cash inside the brokerage account: £77.40.** The "35% cash" refers to a separate high-yield savings account not visible in the portfolio feed. Not reconciled — TODO.
  - **Bonds 4.4%** (£1,937: BB3M £1,400 + EMHG £537), not 10%.
  - Currency mix: USD 50.4% / GBP 43.7% / CHF 3.2% / EUR 2.7%.
- **Concentration:** top 5 = **40.9%**, top 10 = **59.5%**. Largest position VUAG at **16.2%** — through the 10% single-position ceiling.
- **Thematic exposure — never previously measured, and it surprised him:** pure semiconductors **21.5%** (SEMI, NVDA, AMD, MRVL); AI and adjacent **31.0%**; financials **24.2%**. Ivan described AI as a macro theme he believes in. It is already a third of the book. The theme is not a plan; it is the existing position.
- **The long tail, quantified:** **50 positions each under 0.5% of the book**, together only 12.3% of value. **47 of them were opened on a single day — 2026-04-30 — at an average of £108 each.** That one afternoon created 64% of his total position count and 11.5% of value, aggregate P/L +£328. This is the attention problem made concrete: 74 positions against 2 hrs/week is **1.6 minutes per holding per week**.
- **Fund vs direct holdings:** five pooled funds make up **£13,968, 31.8% of the book** (VUAG £7,108 / SEMI £4,284 / BB3M £1,400 / IHCU £639 / EMHG £537). The remaining 68% is held in direct lines.
- **Book split (stated):** 25% tactical / 45% core / 30% generational.
- **Book split (actual):** One book, not three. **Zero holdings sold in the last twelve months** — everything is generational by default rather than by decision. No target weights, no trim discipline, no sell triggers. "Rebalance" means "decide in the moment," which under stress means "do nothing."
- **Separate trading account:** CFDs, options, leveraged ETFs. Deliberately small, mentally quarantined. **This is where the margin lives** — it resolves the undisclosed-leverage question from the first interview.
- **Position count vs attention:** **74 positions against 2 hrs/week is 1.6 minutes per holding per week**, most of it watching prices rather than researching. FX and dealing costs materially erode a book of this many small positions.
- **Reporting currency:** GBP. Living costs will be HKD. Currency mismatch is inertia, not a decision. TODO: decide deliberately.
- **Margin is in use.** Disclosed indirectly during stress testing, not volunteered. Extent unknown. TODO: quantify.
- **Cash (resolved 2026-08-08):** 35% held in high yield **for its yield — explicitly not dry powder.** Reviewed when the savings rate moves, not when the market falls. This is a deliberate decision, superseding the earlier "reserve to capture opportunity / unsure what to do with it" framing. A 30% market fall does not open the cash book.
- **Cash floor:** £8,000 minimum ring-fenced outside the portfolio (~6 months of projected HK expenses). Raised, not lowered, while unemployed. The portfolio is never sold to cover living costs.

### Live positions of note
- **Figma — AUDITED: -78.4%.** Cost £452.96, now worth **£97.95**, unrealised loss **-£355.01**. Opened **2025-07-31** at the IPO on AI-driven expectations. Held for a full twelve months with no action taken. The archetype of the FOMO trigger, and the reason the 72-hour cooldown rule exists. The pattern to note: Ivan names "buying every IPO" as his biggest mistake while the position from that mistake stays open. His largest single loss, and his response to it was not panic but **paralysis** — which materially revises the drawdown-tolerance picture.
- **Losers across the book — AUDITED:** **25 of 74 positions underwater, £1,571 unrealised loss in aggregate. No stop appears to have fired on any of them.** Worst: Figma -78.4%, Marvell -22.1% (-£442, opened 2026-06-17), Reddit -19.6% (-£366, opened 2024-03-22).
- **Short SpaceX (2026-08).** Entered on hype around the listing/opening event. Position moved against him after the share price rose. Still held as of 2026-08-08 on the expectation of a Monday fall. No stop applied. On margin. TODO: log the exit and the realised P&L.

### Behavioural patterns observed
- **Hype is the entry trigger.** Self-identified. The SpaceX short was put on "because of the hype."
- **Rules lapse under emotion.** Ivan describes himself as "I am stop loss," yet holds a losing short with no stop and a reversal thesis. Rules hold on calm positions and fail on painful ones.
- **Reform is stated in future tense.** "After exiting this SpaceX position, I will do more research myself instead of looking at momentum and having cool off periods." The stated fix is contingent on the losing trade closing. It has not been adopted.
- **Repeats the same error in new clothing.** Self-declared biggest mistake is "being irrational and buying every IPO." Current live position is a directional trade around a listing event. The mistake changed direction, not process.
- **Hesitation at execution is his oversizing tell.** His words: if he doubts at execution he cuts the lot size because he is "too 'scared' of losing with that volume." This is a body signal, not a judgement call — it is his most reliable self-regulation mechanism.
- **Talks under stress.** Claims positions are not shared with others, but under a 70% drawdown scenario said he "would probably tell a few people." Bad weeks have a social feedback loop that pushes toward action.
- **Deleverages before evaluating.** In the stress test his instinct was to cut size so it "doesn't hit my margin," then evaluate through the week. Instinct is sound; the presence of margin is what makes it necessary.
- **Deadline pressure produces a clean no.** Given a Friday-deadline deal from a friend: "I'd just say no I wanna take my own time." Genuine strength. Protect it.
- **Philosophy and positioning are two different people.** States "time in the market beats anything" and "long-only core, I don't chase names I don't hold" while running a leveraged short on a name he doesn't own, with a days-long horizon.
- **Drawdown tolerance is unmeasured.** Two different figures given: £10k hurts (~12–15% of capital) and -27%. **Worst loss ever actually carried is ~£3k — noise, not a drawdown.** Both numbers are untested estimates. Do not treat either as a reliable input.
- **Paralysis, not panic.** Revised 2026-08-08 on audited data. Figma sat at -78% for twelve months with no action. 25 underwater positions, no stops fired. His stated fear was selling in a drop; the evidence says he does nothing at all. **Advice framed around "don't panic sell" is aimed at the wrong failure mode.** The intervention he needs is a forcing function to decide, not a restraint on acting.
- **Buys in bursts.** 47 positions opened on a single day (2026-04-30). Combined with zero disposals, the book grows monotonically by episode. Position count only ever goes up.
- **Never sells.** Zero disposals in twelve months. Combined with no sell triggers and no target weights, the practical risk is not panic-selling — it is inertia, and holding losers indefinitely (Figma, SpaceX) while calling it conviction.
- **Sources.** Trusted: Bloomberg, FT, Yahoo Finance. Discounted: tabloids, consumer magazines, anything arriving as a story rather than a number. The stated defence is screening on P/E, cash flows and fundamentals; the failure mode is when a story arrives before the screen does.

### Active themes & views (rough, not committed)
- **AI as a portfolio driver** — believes macro themes, AI specifically, will drive portfolio growth. Not yet expressed as a sized, dated, or falsifiable thesis.
- **Neither concentrated nor diversified by conviction** — "too diversified sees less gross returns and too concentrated is too high risk." A midpoint by default rather than by design. Note: Ivan works in wealth management; check this is his view, not house view.

### Standing constraints
- No crypto, no digital assets, no meme coins, no penny stocks.
- No investments sourced from group chats or unsolicited tips.
- Self-carved exception: up to ~5% of capital into a friend's company "to support" them. This is a social thesis, not an investment one. Flagged as the softest point in the anti-portfolio.
- Deadline-pressured deals get an automatic no.
- **Compliance (unaddressed).** A Hong Kong wealth management or trading role will impose personal account dealing rules — pre-clearance, restricted lists, minimum holding periods. Current structure (margin, shorts, event-driven trading) may not survive day one of employment. Ivan answered "no" when asked about compliance constraints; this is a gap in awareness, not an absence of constraint.

### Known unknowns — TODO
- Actual London monthly expenses. Ivan estimated £2.5–2.8k but said "not sure." **This is now the only remaining unknown on the income statement** — net pay is confirmed at £3,711.
- Actual expected Hong Kong monthly expenses. Estimated £1.2–1.5k, also "not sure."
- Net monthly deployment capacity. Ivan answered "no financial issues," which is not a number — it means variance is being absorbed by family support.
- Size and terms of margin facility.
- Size and terms of family support from September.

---

## Change log

**2026-08-08** — **Tax and capital gains removed from scope** at Ivan's instruction. Stripped from `memory.md`, `instructions.md`, `investor-one-pager.md`, `pnl-summary.md`, the rule engine in `prompts.py`, and the dashboard. The former fund-domicile breach check was repointed at fund/direct-line overlap on the same holdings. The London→Hong Kong relocation is retained as a life event (it drives income, expenses and the cash floor); employer compliance and personal account dealing rules are retained as a regulatory constraint. **Do not reintroduce tax reasoning.** Dashboard renamed to **Maple**.

**2026-08-08** — **One-pager v3 — figures audited against live Trading 212 data.** Built a Streamlit dashboard (`app.py`) with a deterministic Python rule engine (`prompts.diagnostics`) reading `data/portfolio.json`. Running it against the real book corrected almost every number Ivan had given from memory: **74 positions not 57**; account value **£44,051** (not the £50–100k capital band, which includes savings held elsewhere); **bonds 4.4% not 10%**; **cash in-account £77**, with the 35% cash sitting in a separate unreconciled savings account. New findings never previously measured: **fund holdings £13,968 = 31.8%** across five pooled funds; **top-5 concentration 40.9%** with VUAG at 16.2% through the 10% ceiling; **AI/semis exposure 31%/21.5%**; **50 positions under 0.5%**, of which **47 were opened on one day (2026-04-30)**; **25 losers totalling -£1,571 with no stops fired**; **Figma -78.4% held twelve months**. Added rules 17–19 (no new positions above a count of 30; written one-line reason per holding; quarterly thematic measurement) and a priority queue. Behavioural revision: his drawdown response is **paralysis, not panic**.

**2026-08-08** — **One-pager v2.** Merged the standalone `investment_folder/investor-one-pager-ivan.md` into `investor-os/investor-one-pager.md`; the duplicate was deleted so a single authoritative copy remains. Four conflicts resolved by Ivan: (1) citizenship corrected to **US/British/HK**; (2) North Star set to "exit first, retirement underneath"; (3) cash confirmed as held-for-yield, not dry powder; (4) merged doc kept only in `investor-os`. New facts absorbed from the older doc: 57 positions, separate CFD/options/leveraged-ETF trading account (source of the margin), Figma still held from IPO, zero disposals in 12 months, ~£3k worst realised loss vs a stated -27% tolerance, 2 hrs/week attention budget, no rebalancing rule. Cooldown reconciled to 72 hours baseline / 7 days for hype-sourced ideas.

**2026-08-08** — Net salary confirmed at £3,711/mo (was estimated £3,100). Net to portfolio revised up to ~£1,061/mo.

**2026-08-08** — Maple investor-os folder initialised. One-pager v1 finalised. Interview completed across seven phases plus two stress tests. Margin use and stress-talking disclosed during stress testing, not during direct questioning — both added to behavioural patterns. Compliance flagged as a live unaddressed constraint. Live short SpaceX position logged as open.

*New entries go above this line, dated, in reverse chronological order.*
