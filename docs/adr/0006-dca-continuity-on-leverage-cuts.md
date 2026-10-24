# 0006. Full-Cash DCA Continuity on Delisting Leverage Cuts

When a contract facing delisting has its maximum leverage reduced by the exchange (e.g. from 10x to 2x or 1x), we do not cancel pending DCA buy orders; instead, we allow them to execute with full cash margin requirements as long as account free margin permits. This preserves the uninterrupted mathematical grid recovery chain, accepting higher single-symbol cash margin consumption in exchange for eliminating premature position or grid truncation ahead of terminal settlement.
