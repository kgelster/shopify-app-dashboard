"""The numbers-don't-match questions, answered once.

Modelled on Mixpanel's per-report FAQ pages, which exist for one reason: almost
every support question an analytics product gets is "why don't these two numbers
agree", and almost every answer is "they measure different things and both are
right". Writing that down once is cheaper than explaining it each time, and it
is the difference between a reader distrusting the dashboard and a reader
understanding it.

Plain prose, no markup. The same list renders as HTML at /faq and as markdown at
/faq.md, so anything that needed escaping in one and not the other would be a
bug waiting to happen. Questions are ordered by how often they actually come up.
"""

FAQ: list[tuple[str, list[str]]] = [
    (
        "Why don't MRR and collected revenue match?",
        [
            "They are not the same measurement and they never will be. MRR is a "
            "projection of a state: what the subscriptions that exist right now "
            "are worth per month. Collected revenue is a record of what moved: "
            "what Shopify actually billed and what actually reached the payout.",

            "So a merchant who pays $120 up front for a year appears in "
            "collected revenue once, as $120 in the month they paid, and in MRR "
            "every month as $10. A merchant who churned yesterday is gone from "
            "MRR and still in every month of collected revenue they ever paid "
            "for.",

            "Both are on Overview on purpose. If you want to know what the "
            "business is worth per month, read MRR. If you want to know what "
            "landed in the bank, read collected.",
        ],
    ),
    (
        "Why does an annual plan add so little to MRR?",
        [
            "Because MRR is monthly by definition, so an annual plan counts as "
            "one twelfth of its price. A $120/year plan is $10 of monthly "
            "recurring revenue. Counting it at $120 would make one annual "
            "subscriber look like a dozen monthly ones and make MRR jump and "
            "collapse on the anniversary.",

            "The whole amount does show up, in collected revenue, in the month "
            "it was billed.",

            "This only works if the dashboard knows which prices are annual. "
            "AppSubscription carries no billing-interval field, so annual plans "
            "are recognised by price via ANNUAL_PLAN_AMOUNTS. A price missing "
            "from that list is treated as monthly, which is exactly the twelve-"
            "times overstatement described above.",
        ],
    ),
    (
        "Why do the uninstall reasons add up to more than the uninstalls?",
        [
            "Shopify's exit survey is multi-select. One merchant can tick "
            "\"too expensive\" and \"missing features\" and the bars count both, "
            "so the bars total more merchants than actually left.",

            "The bars also cover a shorter span than the uninstall table. "
            "Shopify only made answering mandatory partway through 2026, and "
            "far fewer merchants answered while it was optional. Pooling the "
            "two eras would bury a near-census under a mostly empty "
            "denominator, so the bars count the mandatory era only and the "
            "Churn page states both halves next to them. The boundary date is "
            "REASON_MANDATORY_FROM, which is configured rather than reported by "
            "Shopify: check it against your own feed.",
        ],
    ),
    (
        "Why is a shop in the churn table but still listed as installed?",
        [
            "Because it came back. The churn table has one row per uninstall "
            "event, and a merchant who left in March and reinstalled in June has "
            "a March row and a current state of installed.",

            "That is also why the days column on a churn row measures the stay "
            "that ended rather than the time since the shop first installed. A "
            "shop that installed twice reports each stay separately.",
        ],
    ),
    (
        "Why doesn't the uninstall count match the number of merchants who left?",
        [
            "Two different things arrive as an uninstall. A merchant choosing to "
            "remove the app, and Shopify closing or freezing the store. The raw "
            "feed calls both of them the same thing.",

            "Anything on this dashboard about why merchants leave counts only the "
            "first kind, because a store Shopify closed never chose to go and was "
            "never shown the exit survey. The second kind is counted separately "
            "on the Churn page, under stores that died. The Overview uninstall "
            "tile is the one place both are added together.",
        ],
    ),
    (
        "Why does GA4 report fewer installs than the Partner API?",
        [
            "Because GA4 counts a browser event and the Partner API counts the "
            "install itself. Consent banners, ad blockers, tracking blockers and "
            "EU traffic all suppress the browser event while the install still "
            "happens.",

            "The Partner API is the truth. The Traffic page states the gap "
            "outright, which is what turns a listing conversion rate that looks "
            "like a product problem into a known measurement one. Every "
            "conversion rate computed from GA4 is a floor, not an estimate.",
        ],
    ),
    (
        "Why did a historical number change when nothing new happened?",
        [
            "Derivation is a full replay. The raw Partner API feed is append-only "
            "and immutable, and every table the dashboard reads is rebuilt from "
            "it. So a fix to the derivation logic rewrites history, on purpose: "
            "the annual-plan interval fix corrected months of MRR that had been "
            "recorded at twelve times their real value.",

            "This is why money is computed from subscriptions joined to charges "
            "rather than from the net_change field on the raw events. Those "
            "values were recorded before the fix and still carry the old "
            "inflated figures.",
        ],
    ),
    (
        "Which number should I trust when two pages disagree?",
        [
            "Check what each one is counting before assuming one is broken. "
            "Nearly every disagreement on this dashboard is one of the cases "
            "above: a projection against cash, a point in time against a window, "
            "merchant-chosen exits against every uninstall, or a browser event "
            "against a server record.",

            "Every tile carries its own definition and the table it comes from. "
            "If two numbers still disagree after reading both definitions, that "
            "is a bug worth reporting.",
        ],
    ),
]
