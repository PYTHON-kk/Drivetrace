"""Lumped electro-thermal EV model with BMS logic and fault injection (FR-SIM-01/02).

Re-exports the declared vehicle physical model from models/vehicle_model.py for the simulator harness.
"""

from physics.thermal_model import (  # noqa: F401
    G,
    AIR_DENSITY,
    VehicleParams,
    Faults,
    VehicleState,
    StepResult,
    ocv_pack,
    resistance,
    solve_current,
    VehicleSim,
)
