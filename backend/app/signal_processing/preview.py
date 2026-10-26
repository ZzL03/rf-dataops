import numpy as np
from numpy.typing import NDArray

from app.core.errors import DomainError


def build_preview(iq: NDArray[np.complex64], start: int, sample_rate: float, points: int) -> dict:
    """Return a bounded display preview; waveform thinning is not scientific resampling."""
    if not np.isfinite(iq).all():
        raise DomainError("NONFINITE_IQ", "Preview interval contains NaN or infinity")
    offsets = np.linspace(0, len(iq) - 1, min(points, len(iq)), dtype=int)
    shown = iq[offsets]
    n = min(len(iq), 4096)
    window = np.hanning(n) if n > 2 else np.ones(n)
    transformed = np.fft.fftshift(np.fft.fft(iq[:n] * window))
    power = np.abs(transformed) ** 2 / max(float(np.sum(window**2)), 1.0)
    frequency = np.fft.fftshift(np.fft.fftfreq(n, d=1 / sample_rate))
    bins = np.linspace(0, n - 1, min(points, n), dtype=int)
    magnitude = np.abs(iq.astype(np.complex128))
    mean_power = float(np.mean(magnitude**2))
    return {
        "sample_indices": (start + offsets).tolist(),
        "i": shown.real.astype(float).tolist(),
        "q": shown.imag.astype(float).tolist(),
        "frequency_offset_hz": frequency[bins].tolist(),
        "power_db": (10 * np.log10(np.maximum(power[bins], 1e-20))).tolist(),
        "stats": {
            "mean_power": mean_power,
            "rms": float(np.sqrt(mean_power)),
            "peak_amplitude": float(magnitude.max()),
            "dc_i": float(np.mean(iq.real, dtype=np.float64)),
            "dc_q": float(np.mean(iq.imag, dtype=np.float64)),
        },
        "fft_samples": n,
        "power_reference": "relative units; uncalibrated windowed periodogram",
    }
