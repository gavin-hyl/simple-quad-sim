import subprocess

VALID_TRAJS = ["hover", "circle", "figure8"]
VALID_WINDS = ["const", "sin"]
WIND_MAGS = [0, 1, 3, 5, 10]

for traj in VALID_TRAJS:
    for wind in VALID_WINDS:
        for wind_mag in WIND_MAGS:
            subprocess.run(["python", "-u", "simple-quad-sim/sim.py", "--traj", traj, "--wind", wind, "--wind_mag", str(wind_mag)])
