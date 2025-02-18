import subprocess
from params import VALID_TRAJS, VALID_WINDS, WIND_MAGS

for traj in VALID_TRAJS:
    for wind in VALID_WINDS:
        for wind_mag in WIND_MAGS:
            subprocess.run(
                [
                    "python",
                    "-u",
                    "simple-quad-sim/sim.py",
                    "--traj",
                    traj,
                    "--wind",
                    wind,
                    "--wind_mag",
                    str(wind_mag),
                ]
            )
