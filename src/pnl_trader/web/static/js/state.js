// Centralized UI and Data State Store for Dashboard
export const state = {
  allPositions: [],
  currentTierFilter: 'ALL',
  currentPeriod: '7d',
  chartHistoryPoints: [],
  ledgerAvailable: false,
  tradingUptimeBaseSeconds: null,
  tradingUptimeUpdatedAt: 0,
  currentView: 'positions',
  rawBuyFills: [],
  rawSellFills: [],
  currentSortKey: 'tier',
  currentSortDirection: 'desc',
};
