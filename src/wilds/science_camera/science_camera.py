"""
ScienceCamera — high-level async facade for an Archon CCD controller.

WILDS has two science cameras (VIS and UV) that may share a single
ArchonController if both CCDs are driven by one Archon box.

Usage::

    from archon.controller.controller import ArchonController
    from wilds.science_camera import ScienceCamera

    ctrl = ArchonController("wilds", host="archon.wilds.local")
    vis = ScienceCamera("vis", ctrl, temp_status_key="mod2/tempa")
    uv  = ScienceCamera("uv",  ctrl, temp_status_key="mod2/tempb")

    await vis.startup(acf_file="wilds.acf")   # connect, load ACF, power on, flush
    frame = await vis.expose(300.0)            # numpy array
"""

import os
import pathlib
from datetime import datetime, timezone

import numpy as np
from astropy.io import fits
from pydantic import BaseModel, ConfigDict

from archon.controller.controller import ArchonController
from archon.controller.maskbits import ArchonPower, ControllerStatus
from wilds.bridge.telescope import TelescopeStatus


def _add_telescope_headers(header: fits.Header, ts: TelescopeStatus) -> None:
    if ts.target_name:
        header["OBJECT"] = (ts.target_name, "Target name")
    if ts.airmass is not None:
        header["AIRMASS"] = (ts.airmass, "Airmass at start of exposure")
    if ts.par_angle is not None:
        header["PARANG"] = (ts.par_angle, "[deg] Parallactic angle")
    if ts.lst is not None:
        header["LST"] = (ts.lst, "Local sidereal time")
    if p := ts.pointing:
        ra = p.currentRADec.ra
        if ra.hours is not None:
            ra_deg = (ra.hours + (ra.minutesTime or 0) / 60 + (ra.secondsTime or 0) / 3600) * 15
            header["RA"] = (ra_deg, "[deg] Right ascension")
        dec = p.currentRADec.declination
        if dec.degreesDec is not None:
            d = int(dec.degreesDec)
            sign = -1 if dec.degreesDec.startswith("-") else 1
            dec_deg = sign * (abs(d) + (dec.minutesArc or 0) / 60 + (dec.secondsArc or 0) / 3600)
            header["DEC"] = (dec_deg, "[deg] Declination")
        az = p.currentAzEl.azimuth
        if az.degreesArc is not None:
            az_deg = az.degreesArc + (az.minutesArc or 0) / 60 + (az.secondsArc or 0) / 3600
            header["AZ"] = (az_deg, "[deg] Azimuth")
        el = p.currentAzEl.elevation
        if el.degreesAlt is not None:
            el_deg = el.degreesAlt + (el.minutesArc or 0) / 60 + (el.secondsArc or 0) / 3600
            header["EL"] = (el_deg, "[deg] Elevation")
        if p.currentRotatorPositions and p.currentRotatorPositions.rotPA is not None:
            header["ROTPA"] = (p.currentRotatorPositions.rotPA, "[deg] Rotator position angle")


class Exposure(BaseModel):
    """Parameters of the most recently started exposure."""

    model_config = ConfigDict(frozen=True)

    exptime: float      # [s] requested exposure duration
    date_obs: datetime  # UTC timestamp when shutter opened
    folder: pathlib.Path


class CameraStatus(BaseModel):
    """Snapshot of camera state, including temperature from the STATUS command."""

    model_config = ConfigDict(frozen=True)

    controller_flags: list[str]     # ControllerStatus flag names currently set
    is_idle: bool
    is_exposing: bool
    is_reading: bool
    power_state: str                # ArchonPower.name
    ccd_temp: float | None          # °C; None if STATUS key absent or unreadable


class ScienceCamera:
    """
    Async facade for a single CCD on an Archon controller.

    Parameters
    ----------
    name:
        Camera identifier, e.g. ``"vis"`` or ``"uv"``.
    controller:
        ArchonController instance. May be shared between cameras if both CCDs
        are driven by one Archon box.
    temp_status_key:
        Key in the STATUS response dict that holds this CCD's temperature,
        e.g. ``"mod2/tempa"``. Hardware-specific — update once the ACF from
        STA is received.
    heater_target_key:
        ACF keyword for this CCD's heater setpoint,
        e.g. ``"MOD11\\HEATERATARGET"``. Hardware-specific.
    """

    def __init__(
        self,
        name: str,
        controller: ArchonController,
        *,
        temp_status_key: str = "mod2/tempa",
        heater_target_key: str = "MOD11\\HEATERATARGET",
    ) -> None:
        self.name = name
        self._ctrl = controller
        self._temp_key = temp_status_key.lower()    # STATUS keys come back lowercase
        self._heater_key = heater_target_key
        self.latest_exposure: Exposure | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self, acf_file: str | os.PathLike | None = None) -> None:
        """Connect to the controller and initialise.

        If *acf_file* is given, loads it and powers on. If not (e.g. before
        the ACF from STA is available), performs a reset only.
        """
        await self._ctrl.start()
        if acf_file is not None:
            await self._ctrl.write_config(acf_file, applyall=True, poweron=True)
        else:
            await self._ctrl.reset()
        await self._ctrl.flush()

    async def shutdown(self) -> None:
        """Power off the CCD."""
        await self._ctrl.power(False)

    # ------------------------------------------------------------------
    # Exposure control
    # ------------------------------------------------------------------

    async def expose(
        self,
        exptime: float,
        folder: str | os.PathLike,
    ) -> np.ndarray:
        """Expose, read out, and return the frame as a numpy array."""
        self.latest_exposure = Exposure(
            exptime=exptime,
            date_obs=datetime.now(timezone.utc),
            folder=pathlib.Path(folder),
        )
        task = await self._ctrl.expose(exptime, readout=True)
        await task
        buf = await self._ctrl.readout(block=True, idle_after=True)
        assert buf is not None
        return await self._ctrl.fetch(buf)

    async def readout(self) -> np.ndarray:
        """Trigger readout and fetch (used after ``expose(readout=False)``)."""
        buf = await self._ctrl.readout(block=True, idle_after=True)
        assert buf is not None
        return await self._ctrl.fetch(buf)

    async def abort(self) -> None:
        """Abort the current exposure without reading out."""
        await self._ctrl.abort(readout=False)

    async def flush(self, count: int = 2) -> None:
        """Flush accumulated charge from the CCD."""
        await self._ctrl.flush(count=count)

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    async def save_as_fits(
        self,
        frame: np.ndarray,
        *,
        telescope_status: TelescopeStatus | None = None,
    ) -> pathlib.Path:
        """Write *frame* to ``<latest_exposure.folder>/<name>.fits``. Returns the path written."""
        if self.latest_exposure is None:
            raise RuntimeError("no exposure recorded; call expose() first")
        header = fits.Header()
        header["INSTRUME"] = (self.name, "Camera identifier")
        header["DATE-OBS"] = self.latest_exposure.date_obs.isoformat(timespec="seconds")
        header["EXPTIME"] = (self.latest_exposure.exptime, "[s] Exposure duration")
        device_status = await self._ctrl.get_device_status()
        if raw := device_status.get(self._temp_key):
            try:
                header["CCD-TEMP"] = (float(raw), "[degC] CCD temperature at readout")
            except (ValueError, TypeError):
                pass
        if telescope_status is not None:
            _add_telescope_headers(header, telescope_status)
        path = self.latest_exposure.folder / f"{self.name}.fits"
        fits.writeto(path, frame, header=header, overwrite=True)
        return path

    # ------------------------------------------------------------------
    # Readout window / binning
    # ------------------------------------------------------------------

    async def set_window(
        self,
        *,
        hbin: int = 1,
        vbin: int = 1,
        lines: int | None = None,
        pixels: int | None = None,
        overscan_lines: int | None = None,
        overscan_pixels: int | None = None,
    ) -> dict[str, int]:
        """Set the readout window and binning. Returns the applied window dict."""
        return await self._ctrl.set_window(
            lines=lines,
            pixels=pixels,
            overscanlines=overscan_lines,
            overscanpixels=overscan_pixels,
            hbin=hbin,
            vbin=vbin,
        )

    # ------------------------------------------------------------------
    # Power
    # ------------------------------------------------------------------

    async def power_on(self) -> None:
        """Power on the CCD array."""
        await self._ctrl.power(True)

    async def power_off(self) -> None:
        """Power off the CCD array."""
        await self._ctrl.power(False)

    # ------------------------------------------------------------------
    # Cryogenics
    # ------------------------------------------------------------------

    async def set_temp_setpoint(self, celsius: float) -> None:
        """Write a new CCD temperature setpoint to the controller config.

        The heater keyword (``heater_target_key``) is hardware-specific and
        will need updating once the ACF file from STA is received.
        """
        await self._ctrl.write_line(self._heater_key, celsius)

    # ------------------------------------------------------------------
    # State — synchronous (controller.status is updated internally)
    # ------------------------------------------------------------------

    @property
    def controller_status(self) -> ControllerStatus:
        return self._ctrl.status

    @property
    def is_idle(self) -> bool:
        return bool(self._ctrl.status & ControllerStatus.IDLE)

    @property
    def is_exposing(self) -> bool:
        return bool(self._ctrl.status & ControllerStatus.EXPOSING)

    @property
    def is_reading(self) -> bool:
        return bool(self._ctrl.status & ControllerStatus.READING)

    @property
    def power_state(self) -> ArchonPower:
        s = self._ctrl.status
        if s & ControllerStatus.POWERON:
            return ArchonPower.ON
        if s & ControllerStatus.POWEROFF:
            return ArchonPower.OFF
        if s & ControllerStatus.POWERBAD:
            return ArchonPower.UNKNOWN
        return ArchonPower.UNKNOWN

    # ------------------------------------------------------------------
    # Integrated status snapshot (async — requires STATUS command round-trip)
    # ------------------------------------------------------------------

    async def get_status(self) -> CameraStatus:
        """Return a snapshot combining controller flags and CCD temperature."""
        status = self._ctrl.status
        device_status = await self._ctrl.get_device_status()

        ccd_temp = None
        if raw := device_status.get(self._temp_key):
            try:
                ccd_temp = float(raw)
            except (ValueError, TypeError):
                pass

        return CameraStatus(
            controller_flags=[f.name for f in status.get_flags() if f.name],
            is_idle=bool(status & ControllerStatus.IDLE),
            is_exposing=bool(status & ControllerStatus.EXPOSING),
            is_reading=bool(status & ControllerStatus.READING),
            power_state=self.power_state.name,
            ccd_temp=ccd_temp,
        )
