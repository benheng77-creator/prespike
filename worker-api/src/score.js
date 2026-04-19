const clip = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
const pos = (x) => Math.max(0, x);
const sig = (z) => (z >= 0 ? 1 / (1 + Math.exp(-z)) : Math.exp(z) / (1 + Math.exp(z)));

const REQUIRED = [
  "NewsSent", "SocialSent", "FlowSent", "Freshness", "Coverage",
  "Samples", "Regime", "ShrinkN", "BaseWR", "D5", "D15", "D60", "D240",
  "DecisionPct", "ConfidencePct",
  "EventRiskScore", "DriftScore",
  "EntryPx", "StopPx", "TargetPx", "FeeR", "SlipR",
];

export function computeScore(raw) {
  const missing = REQUIRED.filter((k) => raw[k] === undefined || raw[k] === null);
  if (missing.length) throw new Error(`missing fields: ${missing.join(",")}`);

  const i = {
    VetoReason: "",
    ExecMin: 72,
    WatchMin: 58,
    ...raw,
  };

  const RawSent = clip(0.50 * i.NewsSent + 0.30 * i.SocialSent + 0.20 * i.FlowSent, -1, 1);
  const SentQuality = clip(Math.sqrt(Math.max(0, i.Freshness * i.Coverage)), 0, 1);
  const SentimentPct = clip(50 + 50 * RawSent * SentQuality - 18 * (1 - SentQuality), 0, 100);

  const denom = Math.max(1, i.Samples + i.ShrinkN);
  const RawTrend = (i.Samples * i.Regime + i.ShrinkN * i.BaseWR) / denom;
  const AdaptiveTrendPct = clip(100 * sig(12 * (RawTrend - 0.5)), 0, 100);

  const MTFConflict =
    (Math.abs(i.D5 - i.D15) + Math.abs(i.D15 - i.D60) + Math.abs(i.D60 - i.D240)) / 6;
  const MTFRaw = 0.25 * i.D5 + 0.35 * i.D15 + 0.25 * i.D60 + 0.15 * i.D240;
  const MTFConfirmPct = clip((50 + 50 * MTFRaw) * (1 - 0.35 * MTFConflict), 0, 100);

  const CommercialPct = clip(
    0.50 * i.DecisionPct +
      0.15 * SentimentPct +
      0.20 * AdaptiveTrendPct +
      0.15 * MTFConfirmPct,
    0,
    100,
  );
  const RiskPenalty =
    100 *
    (0.08 * Math.pow(pos(i.EventRiskScore - 0.45), 1.35) +
      0.10 * Math.pow(pos(i.DriftScore - 0.60), 1.40));

  const ConfPlusPct = i.VetoReason
    ? 0
    : clip(
        0.62 * i.ConfidencePct +
          0.18 * MTFConfirmPct +
          0.20 * AdaptiveTrendPct -
          RiskPenalty,
        0,
        99,
      );

  const z =
    -2.20 +
    0.035 * ConfPlusPct +
    0.020 * CommercialPct +
    0.012 * MTFConfirmPct +
    0.010 * AdaptiveTrendPct +
    0.008 * SentimentPct -
    1.40 * i.EventRiskScore -
    1.20 * i.DriftScore;
  const PWinPct = clip(100 * sig(z), 5, 95);

  const stopDist = Math.abs(i.EntryPx - i.StopPx);
  const tgtDist = Math.abs(i.TargetPx - i.EntryPx);
  const RRTrue = clip(tgtDist / Math.max(1e-6, stopDist), 0, 6);

  const CostR = Math.max(
    0,
    i.FeeR + i.SlipR + 0.20 * pos(i.EventRiskScore - 0.50) + 0.25 * pos(i.DriftScore - 0.65),
  );

  const p = PWinPct / 100;
  const EV_R = p * RRTrue - (1 - p) - CostR;
  const EdgePct = clip(50 + 50 * Math.tanh(1.35 * EV_R), 0, 100);

  const BaseScore =
    0.28 * CommercialPct +
    0.27 * ConfPlusPct +
    0.18 * MTFConfirmPct +
    0.10 * SentimentPct +
    0.17 * EdgePct;
  const HardPenalty =
    30 * Math.pow(pos(i.EventRiskScore - 0.60), 1.50) +
    24 * Math.pow(pos(i.DriftScore - 0.70), 1.50) +
    20 * Math.pow(pos(0.90 - SentQuality), 1.25);
  const ScoreTotal = clip(Math.round((BaseScore - HardPenalty) * 100) / 100, 0, 100);

  let action;
  if (i.VetoReason) action = "NO TRADE";
  else if (SentQuality < 0.35) action = "NO TRADE";
  else if (stopDist <= 0) action = "NO TRADE";
  else if (tgtDist <= 0) action = "NO TRADE";
  else if (PWinPct < 50) action = "NO TRADE";
  else if (RRTrue < 1.20) action = "NO TRADE";
  else if (EV_R <= 0) action = "NO TRADE";
  else if (
    ScoreTotal >= Math.max(72, i.ExecMin) &&
    CommercialPct >= 68 &&
    ConfPlusPct >= 66 &&
    EV_R >= 0.25
  )
    action = "EXECUTE";
  else if (ScoreTotal >= Math.max(58, i.WatchMin) && EV_R > 0 && PWinPct >= 47)
    action = "WATCH";
  else action = "NO TRADE";

  return {
    RawSent, SentQuality, SentimentPct,
    RawTrend, AdaptiveTrendPct,
    MTFConflict, MTFConfirmPct,
    CommercialPct, RiskPenalty, ConfPlusPct,
    PWinPct, RRTrue, CostR, EV_R, EdgePct,
    BaseScore, HardPenalty, ScoreTotal,
    TerminalAction: action,
  };
}
