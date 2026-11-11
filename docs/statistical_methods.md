# Statistical Methods and Assumptions

## Standard Mann-Kendall vs Modified Mann-Kendall

- Standard Mann-Kendall tests for monotonic trend using the Mann-Kendall S statistic and Kendall tau. It is useful for a raw directional signal and is robust to non-normality, but its p-value is derived under a standard independence approximation.
- Modified Mann-Kendall is an autocorrelation-aware variance correction intended for highly serially dependent series. This project implements the Hamed-Rao variance correction as an optional path, not a silent replacement for the standard result. The original S and tau statistics are preserved, and the variance used for inference is corrected only when the data are long enough and the autocorrelation structure is strong enough to justify it.
- We do not automatically prewhiten the series. The original observations remain the basis for trend direction, and the variance correction is used only when the required sample and autocorrelation conditions are satisfied.
- If the sample is too short, the autocorrelation is weak, or the variance correction is non-positive, the method returns a conservative not-applicable result and the standard method remains the reported default.

## Standard vs Seasonal Mann-Kendall

- Standard Mann-Kendall examines all valid observations in chronological order and is appropriate for a raw time-series direction test.
- Seasonal Mann-Kendall is a separate mode that compares the same calendar month across the available years. It is useful when a seasonal cycle is present and a separate monthly signal is of interest.
- It is not a daily trend test and it is not a seasonal decomposition. It is a monthly grouping and test. It requires multiple months, enough years, and enough valid observations per month to avoid overinterpreting sparse seasonal subsets.
- Missing months and unequal sample sizes are handled, but the result should be interpreted cautiously if the seasonal coverage is weak.

## OLS vs HAC Regression

- Ordinary least squares (OLS) provides a trend slope and ordinary confidence interval using actual time in years. It is simple and transparent, but its standard errors assume approximately independent residuals.
- HAC / Newey-West regression adds a robust variance estimator that accounts for serial dependence and heteroskedasticity under weak dependence assumptions. This project calculates HAC inference only when the time spacing is sufficiently regular; irregular spacing prevents a conventional HAC application because the standard equally spaced estimator would be inappropriate.
- OLS and HAC are reported separately, and the ordinary OLS result remains available even when HAC is not calculated.

## When to Use Each Method

- Daily MODIS LST records: use standard Mann-Kendall and OLS as descriptive exploratory measures, but label autocorrelation-heavy series as inconclusive if strong serial dependence is present.
- Monthly MODIS LST series: use standard trend tests for raw directional trend and Seasonal Mann-Kendall as a supplementary monthly-seasonal check.
- Yearly or longer aggregated series: the autocorrelation risk is usually lower, but seasonality and sparse coverage still matter.

## When to Mark Results Inconclusive

- Less than 10 valid observations for modified MK.
- Strong lag-1 autocorrelation in a daily series and no robust variance correction.
- Seasonal data with too few years or too few months represented.
- Irregular observation spacing that makes a standard HAC estimator unsuitable.
- Very low data coverage or many missing observations.
- Constant or near-constant series with no meaningful variation.

## Limitations

- Standard Mann-Kendall and OLS p-values can be too optimistic in autocorrelated series.
- Seasonal Mann-Kendall is not a replacement for daily trend tests and should not be used to infer daily meteorological behavior.
- HAC inference is not a universal fix; it requires a sufficiently regular data structure and weak dependence assumptions.
- Welch period comparisons are descriptive rather than causal and are especially sensitive to autocorrelation in daily data.
- Spatial averaging over large AOIs can still mask local variability; the analysis reports the time series mean, not a full pixel-level climate attribution.
