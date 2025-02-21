import scipy.spatial.transform
import numpy as np
from animate_function import QuadPlotter
import csv
import argparse
from params import VALID_TRAJS, VALID_WINDS, WIND_MAGS
from mlmodel import load_model, Model
from torch import tensor

# ============================================================================
#                      Parse Arguments for Data Collection
# ============================================================================
def parse_args():
    default_traj = VALID_TRAJS[0]
    default_wind = VALID_WINDS[0]
    default_wind_mag = WIND_MAGS[0]

    parser = argparse.ArgumentParser(description="Parse simulation parameters.")

    parser.add_argument(
        "--traj",
        type=str,
        choices=VALID_TRAJS,
        default=default_traj,
        help=f"Trajectory. Valid options: {', '.join(VALID_TRAJS)} (default: {default_traj})",
    )
    parser.add_argument(
        "--wind",
        type=str,
        choices=VALID_WINDS,
        default=default_wind,
        help=f"Wind type. Valid options: {', '.join(VALID_WINDS)} (default: {default_wind})",
    )
    parser.add_argument(
        "--wind_mag",
        type=int,
        choices=WIND_MAGS,
        default=default_wind_mag,
        help=f"Wind magnitude. Valid options: {', '.join(map(str, WIND_MAGS))} (default: {default_wind_mag})",
    )

    args = parser.parse_args()
    return args


args = parse_args()
# ============================================================================
#                    End Parse Arguments for Data Collection
# ============================================================================


def quat_mult(q, p):
    # q * p
    # p,q = [w x y z]
    return np.array(
        [
            p[0] * q[0] - q[1] * p[1] - q[2] * p[2] - q[3] * p[3],
            q[1] * p[0] + q[0] * p[1] + q[2] * p[3] - q[3] * p[2],
            q[2] * p[0] + q[0] * p[2] + q[3] * p[1] - q[1] * p[3],
            q[3] * p[0] + q[0] * p[3] + q[1] * p[2] - q[2] * p[1],
        ]
    )


def quat_conjugate(q):
    return np.array([q[0], -q[1], -q[2], -q[3]])


def quaternion_from_vectors(v_from, v_to):
    v_from = normalized(v_from)
    v_to = normalized(v_to)
    v_mid = normalized(v_from + v_to)
    q = np.array([np.dot(v_from, v_mid), *np.cross(v_from, v_mid)])
    return q


def normalized(v):
    norm = np.linalg.norm(v)
    return v / norm


NO_STATES = 13
IDX_POS_X = 0
IDX_POS_Y = 1
IDX_POS_Z = 2
IDX_VEL_X = 3
IDX_VEL_Y = 4
IDX_VEL_Z = 5
IDX_QUAT_W = 6
IDX_QUAT_X = 7
IDX_QUAT_Y = 8
IDX_QUAT_Z = 9
IDX_OMEGA_X = 10
IDX_OMEGA_Y = 11
IDX_OMEGA_Z = 12


class Robot:
    """
    frames:
        B - body frame
        I - inertial frame
    states:
        p_I - position of the robot in the inertial frame (state[0], state[1], state[2])
        v_I - velocity of the robot in the inertial frame (state[3], state[4], state[5])
        q - orientation of the robot (w=state[6], x=state[7], y=state[8], z=state[9])
        omega - angular velocity of the robot (state[10], state[11], state[12])
    inputs:
        omega_1, omega_2, omega_3, omega_4 - angular velocities of the motors
    """

    def __init__(self):
        self.m = 1.0  # mass of the robot
        self.arm_length = 0.25  # length of the quadcopter arm (motor to center)
        self.height = 0.05  # height of the quadcopter
        self.body_frame = np.array(
            [
                (self.arm_length, 0, 0, 1),
                (0, self.arm_length, 0, 1),
                (-self.arm_length, 0, 0, 1),
                (0, -self.arm_length, 0, 1),
                (0, 0, 0, 1),
                (0, 0, self.height, 1),
            ]
        )

        self.J = 0.025 * np.eye(3)  # [kg m^2]
        self.J_inv = np.linalg.inv(self.J)
        self.constant_thrust = 10e-4
        self.constant_drag = 10e-6
        self.omega_motors = np.array([0.0, 0.0, 0.0, 0.0])
        self.state = self.reset_state_and_input(
            np.array([1.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0, 0.0])
        )
        self.time = 0.0

        self.nf = True
        self.record_data = False

        name = "nf" if self.nf else "pd"
        self.record_path = f"data/{name}_{args.traj}_NF_{args.wind_mag}wind{args.wind}.csv"

        self.tic = 0
        # clear the file
        if self.record_data:
            with open(self.record_path, "w", newline="") as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(
                    [
                        None,
                        "t",
                        "p",
                        "p_d",
                        "v",
                        "v_d",
                        "q",
                        "R",
                        "w",
                        "T_sp",
                        "q_sp",
                        "hover_throttle",
                        "fa",
                        "pwm",
                    ]
                )

        self.p_d_I = 0
        self.v_d_I = 0
        self.a_I = 0
        self.v_I_prev = 0
        self.a_hat = np.zeros(9)
        self.l = 1e-3
        self.P = np.eye(9) * 1e-3
        self.Q = np.eye(9) * 1e-3
        self.R = np.eye(3)
        self.K = np.eye(3)
        if self.nf:
            self.phi_net = load_model("synth-fly_dim-a-3_v-q-pwm-epoch-950")

    def reset_state_and_input(self, init_xyz, init_quat_wxyz):
        state0 = np.zeros(NO_STATES)
        state0[IDX_POS_X : IDX_POS_Z + 1] = init_xyz
        state0[IDX_VEL_X : IDX_VEL_Z + 1] = np.array([0.0, 0.0, 0.0])
        state0[IDX_QUAT_W : IDX_QUAT_Z + 1] = init_quat_wxyz
        state0[IDX_OMEGA_X : IDX_OMEGA_Z + 1] = np.array([0.0, 0.0, 0.0])
        return state0

    def update(self, omegas_motor, dt):
        p_I = self.state[IDX_POS_X : IDX_POS_Z + 1]
        v_I = self.state[IDX_VEL_X : IDX_VEL_Z + 1]
        q = self.state[IDX_QUAT_W : IDX_QUAT_Z + 1]
        omega = self.state[IDX_OMEGA_X : IDX_OMEGA_Z + 1]
        R = scipy.spatial.transform.Rotation.from_quat(
            [q[1], q[2], q[3], q[0]]
        ).as_matrix()

        thrust = self.constant_thrust * np.sum(omegas_motor**2)
        f_b = np.array([0, 0, thrust])

        tau_x = (
            self.constant_thrust
            * (omegas_motor[3] ** 2 - omegas_motor[1] ** 2)
            * 2
            * self.arm_length
        )
        tau_y = (
            self.constant_thrust
            * (omegas_motor[2] ** 2 - omegas_motor[0] ** 2)
            * 2
            * self.arm_length
        )
        tau_z = self.constant_drag * (
            omegas_motor[0] ** 2
            - omegas_motor[1] ** 2
            + omegas_motor[2] ** 2
            - omegas_motor[3] ** 2
        )
        tau_b = np.array([tau_x, tau_y, tau_z])

        v_dot = 1 / self.m * R @ f_b + np.array([0, 0, -9.81]) + self.wind()
        omega_dot = self.J_inv @ (np.cross(self.J @ omega, omega) + tau_b)
        q_dot = 1 / 2 * quat_mult(q, [0, *omega]  )
        p_dot = v_I

        x_dot = np.concatenate([p_dot, v_dot, q_dot, omega_dot])
        self.state += x_dot * dt
        self.state[IDX_QUAT_W : IDX_QUAT_Z + 1] /= np.linalg.norm(
            self.state[IDX_QUAT_W : IDX_QUAT_Z + 1]
        )  # Re-normalize quaternion.
        self.time += dt
        self.tic += 1

        # === Log Data ===
        if self.record_data:
            with open(self.record_path, "a", newline="") as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(
                    [
                        self.tic,
                        self.time,
                        str(p_I.tolist()),
                        str(self.p_d_I.tolist()),
                        str(v_I.tolist()),
                        str(self.v_d_I.tolist()),
                        str(q.tolist()),
                        str(R.tolist()),
                        str(omega.tolist()),
                        None,  # T_sp
                        None,  # q_sp
                        None,  # hover_throttle
                        str(self.wind().tolist()),
                        str(omegas_motor.tolist()),
                    ]
                )
        # === End Log Data ===

        if self.time > 15:
            if self.record_data:
                print(f"data is recorded in {self.record_path}")
            exit("Simulation finished")

    def compute_Phi(self):
        """ Returns a Phi matrix of dimensions (3, 9) """
        x = np.zeros(11)
        x[0:3] = self.state[IDX_VEL_X : IDX_VEL_Z + 1]
        x[3:7] = self.state[IDX_QUAT_W : IDX_QUAT_Z + 1]
        x[7:11] = self.omega_motors
        phi = self.phi_net.phi.forward(tensor(x)).detach().numpy().reshape(1, -1)
        Phi = np.zeros((3, 9))
        for i in range(3):
            Phi[i, i * 3 : (i + 1) * 3] = phi
        return Phi


    def control(self, p_d_I):
        p_I = self.state[IDX_POS_X : IDX_POS_Z + 1]
        v_I = self.state[IDX_VEL_X : IDX_VEL_Z + 1]
        q = self.state[IDX_QUAT_W : IDX_QUAT_Z + 1]
        omega_b = self.state[IDX_OMEGA_X : IDX_OMEGA_Z + 1]

        # Position controller.
        k_p = 2
        k_d = 5
        v_r = -k_p * (p_I - p_d_I)
        s = v_I - v_r
        a = -k_d * s + np.array([0, 0, 9.81])  # original PD controller

        if self.nf:
            Phi = self.compute_Phi()
            a_hat_dot = -self.l * self.a_hat + self.P @ Phi.T @ s
            a_hat_dot += -self.P @ Phi.T @ np.linalg.inv(self.R) @ (Phi @ self.a_hat - self.wind())
            self.a_hat += a_hat_dot * dt

            P_dot = -2 * self.l * self.P + self.Q - self.P @ Phi.T @ np.linalg.inv(self.R) @ Phi @ self.P
            self.P += P_dot * dt
            # compute desired acceleration
            self.a_I = (v_I - self.v_I_prev) / dt
            self.v_I_prev = v_I
            a += self.a_I - Phi @ self.a_hat
            # print("a_hat magnitude: ", np.linalg.norm(self.a_hat))


        f = self.m * a
        f_b = (
            scipy.spatial.transform.Rotation.from_quat([q[1], q[2], q[3], q[0]])
            .as_matrix()
            .T
            @ f
        )
        thrust = np.max([0, f_b[2]])

        # Attitude controller.
        q_ref = quaternion_from_vectors(np.array([0, 0, 1]), normalized(f))
        q_err = quat_mult(quat_conjugate(q_ref), q)  # error from Body to Reference.
        if q_err[0] < 0:
            q_err = -q_err
        k_q = 20.0
        k_omega = 100.0
        omega_ref = -k_q * 2 * q_err[1:]
        alpha = -k_omega * (omega_b - omega_ref)
        tau = self.J @ alpha

        # Compute the motor speeds.
        B = np.array(
            [
                [
                    self.constant_thrust,
                    self.constant_thrust,
                    self.constant_thrust,
                    self.constant_thrust,
                ],
                [
                    0,
                    -self.arm_length * self.constant_thrust,
                    0,
                    self.arm_length * self.constant_thrust,
                ],
                [
                    -self.arm_length * self.constant_thrust,
                    0,
                    self.arm_length * self.constant_thrust,
                    0,
                ],
                [
                    self.constant_drag,
                    -self.constant_drag,
                    self.constant_drag,
                    -self.constant_drag,
                ],
            ]
        )
        B_inv = np.linalg.inv(B)
        omega_motor_square = B_inv @ np.concatenate([np.array([thrust]), tau])
        omega_motor = np.sqrt(np.clip(omega_motor_square, 0, None))

        # === Log Desired Trajectory ===
        self.v_d_I = (p_d_I - self.p_d_I) / dt
        self.p_d_I = p_d_I
        # === End Log Desired Trajectory ===

        self.omega_motors = omega_motor

        return omega_motor

    def wind(self, om=0.5, phi=0):
        if args.wind == "const":
            return np.array([1, 0, 0]) * args.wind_mag
        elif args.wind == "sin":
            return np.array([1, 0, 0]) * args.wind_mag * np.sin(om * self.time + phi)


PLAYBACK_SPEED = 200
CONTROL_FREQUENCY = 200  # Hz for attitude control loop
dt = 1.0 / CONTROL_FREQUENCY
time = [0.0]


def get_pos_full_quadcopter(quad):
    """position returns a 3 x 6 matrix
    where row is [x, y, z] column is m1 m2 m3 m4 origin h
    """
    origin = quad.state[IDX_POS_X : IDX_POS_Z + 1]
    quat = quad.state[IDX_QUAT_W : IDX_QUAT_Z + 1]
    rot = scipy.spatial.transform.Rotation.from_quat(
        quat, scalar_first=True
    ).as_matrix()
    wHb = np.r_[np.c_[rot, origin], np.array([[0, 0, 0, 1]])]
    quadBodyFrame = quad.body_frame.T
    quadWorldFrame = wHb.dot(quadBodyFrame)
    pos_full_quad = quadWorldFrame[0:3]
    return pos_full_quad


def control_propellers(quad):
    t = quad.time
    T = 5
    r = 2 * np.pi * t / T
    if args.traj == "hover":
        p_d_I = np.array([1.0, 0.0, 1.0])
    elif args.traj == "circle":
        p_d_I = np.array([np.cos(r), np.sin(r), 1.0])
    elif args.traj == "figure8":
        p_d_I = np.array([np.cos(r / 2), np.sin(r), 1.0])
    else:
        raise ValueError()
    prop_thrusts = quad.control(p_d_I=p_d_I)
    quad.update(prop_thrusts, dt)


def main():
    print(f"Running {args.traj} with {args.wind} wind of magnitude {args.wind_mag}")
    quadcopter = Robot()

    def control_loop(i):
        for _ in range(PLAYBACK_SPEED):
            control_propellers(quadcopter)
        return get_pos_full_quadcopter(quadcopter)

    plotter = QuadPlotter()
    plotter.plot_animation(control_loop)


if __name__ == "__main__":
    main()
