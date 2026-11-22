# Activation integration review (2026-11-17)

This branch prepares the product reply path to select from current, authorized
owner facts and curated memories after those sources perform their own access
and validity checks. It adds task coverage, scoped boundaries, and a small
context-aware reranker. It does not add contact transcript import, a cloud
embedding provider, or a Cross-Encoder.

Local owner-fact, worker, host integration, companion, and activation tests
re-run against upstream `74f7b36`: **195 passed**.
The tests caught a regression where an explicit question such as “香菜的事还记得吗”
lost the relevant curated fact; the branch now preserves a single distinctive
term match for explicit recall requests.
An additional product regression showed that a relevant disclosure boundary
could be crowded out by the ordinary recall limit. The reply path now reads
current curated rules separately within the resolved memory spaces. A revoked
rule is filtered from both this scoped read and normal curated recall; selected
rules carry an explicit context label. This change did not alter the synthetic
gold-pool diagnostic scores because those pools are small, but it prevents
crowded recall and revoked-state leakage in product integration tests.

An offline TimlMemBench diagnostic supplies *gold current-state candidates*
directly to the activation gate, without testing writing, retrieval, or the
answer model. The initial task/attribute reranker selected the exact set in
**17/63 dev** and **18/82 holdout** cases. Narrower behavior-rule matching
and an explicit meal-choice need raised this to **23/63 dev** and
**23/82 holdout**, with forbidden selections in **12 dev** and **38 holdout**
cases. These figures were reproduced on the current branch. This remains
insufficient for a merge-ready activation PR; the branch is submitted as a
draft for code review and further correction.

November 18 follow-up: keep one-off facts out of future preference replies while
allowing their retention rules in record-keeping questions; narrow disclosure
rules by subject matter, audience, and private-vs-public action. Add a separate
scoped read for retention rules so ordinary Top-K recall cannot crowd them out.
The same equal-pool diagnostic now selects exactly **23/63 dev** and
**33/82 holdout**; forbidden selections fall to **10 dev** and **21 holdout**.
One dev case loses a public nickname rule, so this is still a draft, not a
merge-ready selector. The owner-fact, host, companion, and activation regression
set is **201 passed** after the change. No model answer score is claimed.

For a more comparable exploratory control,
`evals/timlmembench_equal_pool_diagnostic.py` loads the same gold-current
statements as synthetic curated memories and runs each version's product
recall/context path without an answer model. Under this artificial common
pool, improved old v1 selected the exact set in **6/63 dev** and
**4/82 holdout**, while this branch reached **23/63** and **23/82**.
These are *not* the official end-to-end answer scores: the old and new paths
have different memory schemas, all synthetic statements were placed in one
authorized owner space, and matching the selected text back to state IDs is
only a diagnostic. The holdout cases have been inspected during development
and must not be treated as unseen evidence of generalization.

Before proposing integration, inspect the forbidden selections, add cases for
ordinary conversational recall and audience-specific rules, and compare the
end-to-end product path on a fixed benchmark batch. The upstream `owner_fact_v1`
write, forget, and authorization services are unchanged here.
