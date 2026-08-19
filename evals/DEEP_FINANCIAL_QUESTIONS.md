# Deep financial-analysis questions

These prompts are intentionally difficult to answer manually. They require
several scopes, aligned periods, derived mathematics, attribution, missing-data
judgment, or constrained decisions. Run them against a deterministic fixture
before treating them as release gates.

## Answer contract for every case

A strong answer:

1. Leads with the financial conclusion in plain language.
2. States the comparison basis, especially for partial periods.
3. Computes the final baseline, delta, percentage, rank, contribution, or
   scenario result in SQL; the reader never combines intermediate tables.
4. Keeps current value, comparison value, difference, percentage, and driver
   together in one compact table or adjacent callout.
5. Checks transaction direction, refunds, reimbursements, transfers, currency,
   deleted rows, join fanout, missing coverage, and zero denominators.
6. Separates recorded facts from forecasts and assumptions.
7. Explains financial meaning like a patient teacher and avoids database or
   agent vocabulary.
8. Does not claim causation when the ledger only establishes association.

## Complex comparison and attribution

1. **Monthly category baseline and merchant drivers**

   > Compare my Food and Transport spending from May through August 19, group
   > it by month, identify the largest merchants, and compare August with the
   > previous three-month average.

   August must be compared with May 1–19, June 1–19, and July 1–19—not with
   three complete months. Return one row per category with May, June, July,
   August-to-date, comparable baseline, rupee delta, percentage delta, largest
   merchant, merchant amount, and merchant share. Explain which category is
   accelerating faster and which creates the larger rupee impact.

2. **Unusual spending with robust thresholds**

   > Which categories are unusually high this month compared with my normal
   > pace, and which merchants explain at least 80% of each increase?

   Align elapsed days, require both a material rupee delta and percentage
   delta, rank by impact, and avoid calling a category unusual when history is
   missing or the baseline is too small.

3. **Composition shift rather than total change**

   > My total spending looks similar to last month. Which categories quietly
   > replaced others, and what transactions drove the shift?

   Reconcile positive and negative category contributions back to the net
   total change and identify the smallest driver set explaining at least 80%
   of the gross movement.

4. **Merchant price-versus-frequency decomposition**

   > For merchants I used in both periods, was the increase caused by paying
   > more per purchase or buying more often?

   Decompose each merchant's change into transaction-count and average-ticket
   effects, keep new/lost merchants separate, and do not double count the
   interaction term.

5. **Refund-aware true category change**

   > Which categories increased after accounting for refunds and
   > reimbursements, and how different is that from the gross-spend view?

   Show gross spend, returned money, net spend, and the category ranking change
   caused by using the correct definition.

## Forecasting and cash-flow reasoning

6. **Month-end forecast with known obligations**

   > Forecast my month-end cash flow using spending pace, recurring expenses,
   > expected income, EMIs, and bills that have not occurred yet. Show a base
   > case and a conservative case.

   Separate recorded from projected amounts, prevent a recurring payment that
   already occurred from being counted twice, and disclose every scenario
   assumption.

7. **Emergency-fund runway under uncertainty**

   > How many months would my accessible balances last if income stopped,
   > using essential spending plus debt payments and a conservative volatility
   > buffer?

   Do not count investment market value as accessible cash unless the account
   data supports that interpretation. Explain the difference between average
   burn and conservative burn.

8. **Goal feasibility with competing claims**

   > Can I reach my savings goal by its target date while preserving my usual
   > essential spending, loan payments, and a three-month emergency buffer?

   Compute required monthly contribution, historically available surplus,
   shortfall, and the earliest evidence-supported target date. Do not turn a
   projection into a guarantee.

9. **Income shock stress test**

   > If income falls 20% for three months and one month has a ₹50,000 medical
   > expense, which commitments become unaffordable first?

   Apply the shock without changing recorded history, maintain a running cash
   balance, and distinguish saved commitments from inferred discretionary
   spending.

10. **Seasonality-aware forecast**

    > Forecast the next two months by category, but do not let one-off travel,
    > school fees, or annual insurance distort recurring expectations.

    Explain how recurring, seasonal, and one-off components were separated and
    state when the available history is insufficient.

## Constrained decisions and optimization

11. **Constrained spending reduction**

    > Find a realistic way to reduce monthly spending by ₹10,000 without
    > changing Housing, Food, Health, Transport, Education, Bills, or debt
    > payments. Use only costs with evidence of recurrence.

    Treat this as a constrained candidate analysis, not a guarantee. Show each
    candidate, recurrence evidence, expected monthly effect, confidence, and
    cumulative reduction.

12. **Debt prepayment strategy with liquidity protection**

    > Allocate ₹1,00,000 of extra cash across my loans to minimize interest,
    > but preserve a three-month emergency fund and show the effect on payoff
    > time and total interest.

    Compare at least the mathematically optimal allocation with a
    payment-relief alternative. State missing loan terms rather than inventing
    them.

13. **Budget reallocation without raising the total**

    > Reallocate next month's category budgets so the total budget stays the
    > same, essential categories retain adequate headroom, and recent overruns
    > are less likely.

    Derive headroom from comparable history and volatility; show money moved
    from and to each category and prove the changes net to zero.

14. **Purchase affordability with opportunity cost**

    > Can I buy a ₹1,20,000 laptop next month without using debt, missing my
    > savings target, or dropping below my emergency buffer?

    Show available cash after obligations, the funding gap or remaining
    cushion, and the earliest affordable month under the observed savings pace.

15. **Subscription cancellation portfolio**

    > Which combination of subscriptions gives the largest annual saving with
    > the fewest cancellations, excluding services I use frequently?

    Clearly distinguish detected recurring merchants from confirmed
    subscriptions and treat usage frequency as evidence, not preference.

## Reconciliation, data quality, and portfolio reasoning

16. **Possible duplicate and reimbursement chains**

    > Find expenses that may be duplicated across imports or later reimbursed,
    > estimate how much they distort spending, and show why each match is
    > plausible.

    Never silently alter canonical transactions. Keep exact matches,
    probabilistic candidates, and confirmed refunds/reimbursements separate.

17. **Net-worth movement attribution**

    > Explain the change in my net worth between the last two reliable
    > snapshots: how much came from saving, debt reduction, new contributions,
    > and market movement?

    Respect snapshot grain and never sum several valuations of the same account
    or holding within one date bucket.

18. **Investment return versus contributions**

    > My portfolio value increased. How much was actual investment performance
    > versus money I added, and which holdings drove the result?

    Use matched valuation periods, separate cash contributions from returns,
    and abstain from an exact performance claim when required opening values
    are absent.

19. **Coverage-aware anomaly detection**

    > Detect merchants whose amount or frequency is abnormal for me, but ignore
    > anomalies caused only by incomplete months or newly imported history.

    Report the baseline sample size, deviation measure, rupee impact, and the
    evidence that coverage is comparable.

20. **Cross-source plan-versus-actual analysis**

    > Compare actual category spending with my uploaded budget sheet and open
    > vendor invoices, then tell me which categories are truly at risk by
    > month-end.

    Resolve category identities across sources, avoid counting an invoice that
    already became a transaction, and separate current variance from projected
    month-end risk.

## Release recommendation

Start with questions 1, 2, 4, 6, 11, 16, 17, and 20 as the quality-first set.
Run each at least three times and score numerical correctness, tenant route,
comparison alignment, explanation quality, output compactness, latency, and
token cost. Increase reasoning effort beyond `high` only when this suite shows
a repeatable quality gain.
