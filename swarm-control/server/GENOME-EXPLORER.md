# Genome Explorer: visitor journey

Audience: a curious demo-day visitor with little genetics knowledge and limited time.
Desired outcome: recognize a variant, explore a pattern, distinguish observation
from interpretation, and choose a useful next step independently.

| Visitor's starting question | Interface response | Intended next step |
| --- | --- | --- |
| What is this, and is it for me? | Plain-language DNA introduction, no-background-needed invitation, one primary tour action | Start with a small, understandable example |
| Are all these differences bad? | One-letter reference/example illustration; variation framed without health judgments | Understand what the pie chart counts |
| What am I supposed to notice? | Dataset-specific takeaway before metrics; familiar chromosome metaphor and a concrete interaction prompt | Select a chromosome and observe counts changing |
| What is surprising here? | Satellite repeats shown as an explicitly illustrative repeating motif, then measured overlaps | Connect the visual pattern with the analysis |
| Why should I trust it? | Read-depth explanation, unavailable data states, annotation limits, expandable source methods | Separate evidence from conclusions |
| What can I do now? | Three clear choices: another dataset, research workflow, or methods | Follow personal curiosity without a forced funnel |

The sticky navigation reflects scroll position, not comprehension or completion.
Visitors can skip sections, move backward, or inspect sources at any point. No
behavioral tracking or persuasion scores are collected. Genome switching updates
both charts and their introductory takeaway. Numerical differences between datasets
are not presented as a health, ancestry, or diversity ranking.

Validate the journey using `node server/scripts/review-genome-ui.mjs` from
`swarm-control/`. This checks navigation, current-location feedback, keyboard focus
on the next-dataset action, all six datasets, missing-data states and phone/desktop
layouts. These checks establish functionality, not user comprehension; observing
first-time demo visitors remains the next usability research step.
