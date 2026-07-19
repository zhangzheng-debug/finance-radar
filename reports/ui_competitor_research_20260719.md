# Finance Radar professional-terminal UI research

Date: 2026-07-19 (Asia/Shanghai)

## Scope and evidence rule

The research targeted professional financial news flows, scoring/context
workbenches, and cross-asset terminals. Product documentation and first-party
help pages are the primary evidence. Searches on X for Koyfin, Benzinga Pro,
Newsquawk, and financial-terminal UI examples were attempted; public indexing
did not yield stable, attributable design evidence, so screenshots or claims
from individual X posts are not used as requirements.

## Findings

| Reference | Observed workflow | Finance Radar decision |
|---|---|---|
| Bloomberg Terminal UX | hide complexity behind stable, learnable workflows | keep a terse command bar and progressive disclosure |
| TradingView News Flow | live full feed, compound filters, saved flows, keyboard-driven master/detail | retain the feed-first workbench and make saved flows the next interaction feature |
| Benzinga Pro Newsfeed | timestamp, ticker/source/category filters, hover actions, alerts and connection status | keep precise UTC/source metadata; separate workflow state from market direction |
| Newsquawk | scrolling text headlines, optional audio, analyst context | reserve audio for a later evidence-linked experiment |
| Koyfin Markets News | global search, topic tabs, modular news/market context | add context only when fresh and relevant; avoid decorative widgets |
| LSEG Workspace | search-led discovery, news beside cross-asset context, browser/desktop parity | make search universal and keep the narrow layout functionally complete |

## Adopted aesthetic

**Calm Institutional / 冷静机构风**: deep blue-black canvas, restrained borders,
tabular numerics, one cyan action color, and semantic green/amber/red/violet.
There are no gradients, glass panels, decorative neon, fake order tickets, or
single composite “AI score.” The dominant object is a timestamped event with a
traceable evidence edge.

## Primary sources

- https://www.bloomberg.com/company/stories/ux-at-bloomberg-a-conversation-with-fahd-arshad/
- https://www.tradingview.com/support/solutions/43000728828-news-flow-your-daily-hub-for-financial-news/
- https://www.tradingview.com/support/solutions/43000732560-news-flow-s-filters-overview/
- https://www.tradingview.com/support/solutions/43000728826-news-flow-keyboard-navigation/
- https://help.benzinga.com/en/articles/1413278-getting-started-newsfeed
- https://help.benzinga.com/en/articles/1769521-what-is-a-widget
- https://help.benzinga.com/en/articles/2106004-what-is-squawk-and-how-do-i-use-it
- https://www.koyfin.com/help/markets-news/
- https://www.koyfin.com/help/custom-news-screens/
- https://www.newsquawk.com/headlines
- https://www.lseg.com/en/data-analytics/products/workspace

## Current validation

The local release candidate is validated by
`reports/accessibility_local_latest.json` and `.md`: all five pages passed with
no blockers or advisories across desktop and 390px mobile. This machine result
does not claim completion of external screen-reader user testing.
