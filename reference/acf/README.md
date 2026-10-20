# Reference ACF — LVM (SDSS-V Local Volume Mapper)

> **Do not load this onto WILDS hardware.** It is not a WILDS configuration.

`LVM_100kHz.acf` is the Archon configuration that SDSS-V's LVM spectrographs run.
LVM uses the same detector (STA4850) and controller (STA Archon) as WILDS, and the
same `sdss-archon` library, so this file is a working example of what a WILDS ACF
will look like.

- **Source:** [sdss/lvmscp](https://github.com/sdss/lvmscp), `python/lvmscp/etc/LVM_100kHz.acf`
- **Commit:** `94aa336d499d9ccf50e03baeed8a2397933f592f` (file last changed upstream 2023-07-18)
- **Fetched:** 2026-10-20, unmodified
- **License:** BSD-3-Clause, © 2017 SDSS — see `LICENSE-lvmscp.md`

## Why it must not be loaded

An ACF is not just a timing script. It also sets the CCD's bias and clock voltages, and
binds every setting to a module slot on LVM's backplane
(`MOD1_TYPE=12 … MOD12_TYPE=11`, backplane `00003FFF1A986672`).

- Each STA4850 has its own operating voltages, given in its STA test report.
- The WILDS Archons may carry different modules in different slots.

Wrong voltages can damage the detector. The WILDS ACF must come from STA, or be
reviewed against the WILDS devices' test reports and backplane layout.

## What it is useful for

- **Developing `wilds-science-camera`** against the real parameter set, which this file
  shares with what `sdss-archon` expects.
- **Driving a simulator,** or an Archon with no CCD attached.
- **Comparing against the ACF STA supplies** for the WILDS devices.

## What's in it

Timing-script parameters (`PARAMETERn`), all settable with `set_param()`:

- **Exposure:** `Exposures`, `ContinuousExposures`, `ReadOut`, `AbortExposure`,
  `IntCS`, `NoIntMS`, `WaitCount`
- **Flushing:** `DoFlush`, `AutoFlush`, `FlushCount`, `FlushBin`, `SkipLineBinVShift`
- **Window:** `Lines=2040`, `Pixels=2040`, `PreSkipLines`, `PostSkipLines`,
  `PreSkipPixels`, `PostSkipPixels`, `OverscanPixels=20`, `OverScanLines`
- **Binning:** `VerticalBinning`, `HorizontalBinning`
- **Pixel timing:** `PIXEL_CLOCK`, `ST`, `STM1`, `AT`

Geometry: 2040 × 2040 per amplifier is one quadrant of the 4080 × 4080 STA4850, read
through four amplifiers at 100 kHz (`TAPLINES=12`, `LINECOUNT=2040`,
`PIXELCOUNT=2060`).

Heater targets are `MOD12\HEATERATARGET` / `MOD12\HEATERBTARGET` and
`MOD2\HEATERATARGET`. LVM overrides them per controller in `lvmscp.yml`
(`archon.acf_overrides`), the same `write_line` path `ScienceCamera` uses for its
setpoint.
