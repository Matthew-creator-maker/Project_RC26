"""使用假 SDK 验证只读诊断，全程不连接真实机械臂。"""
import contextlib
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

FILE = Path(__file__).with_name('ik_diagnose.py')
if not FILE.exists():
    FILE = Path(__file__).resolve().parents[1]/'tools/ik_diagnose.py'
spec = importlib.util.spec_from_file_location('ik_diagnose', FILE)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class Params:
    def __init__(self, seed, pose, flag):
        assert flag == 1
        self.seed, self.pose = seed, pose


class FakeArm:
    seed = [10, 20, 30, 40, 50, 60]
    predicted = [30, 45, 52, 60, -54, 94]
    def __init__(self):
        self.calls = []
        self.tool_name, self.work_name = 'gripper', 'Base'
    def rm_get_current_tool_frame(self):
        self.calls.append('tool')
        return 0, {'name': self.tool_name, 'pose': [0, 0, 0.1435, 0, 0, 0]}
    def rm_get_current_work_frame(self):
        self.calls.append('work')
        return 0, {'name': self.work_name, 'pose': [0]*6}
    def rm_get_current_arm_state(self):
        self.calls.append('state')
        return 0, {'joint': self.seed, 'pose': [0.2, 0, 0.3, 0, 0, 0]}
    def rm_algo_inverse_kinematics(self, params):
        self.calls.append('ik')
        if params.pose[0] == 0.5:
            return 0, self.predicted
        if params.pose[0] == 0.6 and params.seed != self.predicted:
            return 1, None
        return 0, params.seed
    def rm_algo_ikine_check_joint_position_limit(self, joints):
        self.calls.append('limit')
        return 5 if joints[4] == 150 else 0
    def rm_algo_inverse_kinematics_all(self, _params):
        self.calls.append('all')
        return SimpleNamespace(result=0, num=2, q_solve=[
            [30, 45, 52, 60, -54, 94, 0], [30, 45, 1, 60, 150, 94, 0]])


def report():
    return {'poses_m_rad': {'transition': [0.5, 0, 0, 0, 0, 0], 'final': [0.6, 0, 0, 0, 0, 0]}}


class DiagnosticTests(unittest.TestCase):
    def test_predicted_transition_seed_distinguishes_branch_selection(self):
        arm = FakeArm()
        result = tool.diagnose(arm, Params, {}, report())
        self.assertEqual(result['final_ik_current_seed']['return_code'], 1)
        self.assertEqual(result['final_ik_transition_seed']['return_code'], 0)
        self.assertLessEqual(set(arm.calls), {'tool','work','state','ik','limit','all'})

    def test_all_solutions_retain_limit_and_soft_margin_failures(self):
        result = tool.diagnose(FakeArm(), Params, {}, report())
        solutions = result['final_all_solutions']['solutions']
        self.assertTrue(solutions[0]['j3_margin_ok'])
        self.assertFalse(solutions[1]['j3_margin_ok'])
        self.assertEqual(solutions[1]['joint_limit_return'], 5)

    def test_wrong_tool_stops_queries_before_ik(self):
        arm = FakeArm(); arm.tool_name = 'jiazhua'
        with self.assertRaisesRegex(RuntimeError, '不切换'):
            tool.diagnose(arm, Params, {}, report())
        self.assertNotIn('ik', arm.calls)

    def test_non_base_coordinates_rejected(self):
        arm = FakeArm(); arm.work_name = 'work1'
        with self.assertRaisesRegex(RuntimeError, 'Base'):
            tool.diagnose(arm, Params, {}, report())
        self.assertNotIn('ik', arm.calls)

    def test_missing_all_solve_still_reports_basic_ik(self):
        arm = FakeArm(); arm.rm_algo_inverse_kinematics_all = None
        result = tool.diagnose(arm, Params, {}, report())
        self.assertEqual(result['final_ik_current_seed']['return_code'], 1)
        self.assertIn('无全解', result['final_all_solutions']['error'])

    def test_cli_default_never_imports_sdk(self):
        with patch.object(tool, 'prepare', return_value=({}, report())), \
             patch.object(tool.importlib, 'import_module') as importer, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main(['--xyz-base','0.2','0','0.1']), 0)
        importer.assert_not_called()

    def test_cli_disconnects_after_query_failure(self):
        fake = FakeArm(); fake.tool_name = 'wrong'
        fake.rm_create_robot_arm = lambda *_: SimpleNamespace(id=1)
        closed = []
        fake.rm_delete_robot_arm = lambda: closed.append(True)
        sdk = SimpleNamespace(RoboticArm=lambda _: fake,
                              rm_thread_mode_e=SimpleNamespace(RM_TRIPLE_MODE_E=3),
                              rm_inverse_kinematics_params_t=Params)
        cfg = {'arm': {'ip': 'test.invalid'}}
        with patch.object(tool, 'prepare', return_value=(cfg, report())), \
             patch.object(tool.importlib, 'import_module', return_value=sdk), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main(['--xyz-base','0.2','0','0.1','--connect']), 1)
        self.assertEqual(closed, [True])

    def test_height_profile_overrides_global_pose(self):
        text = '''arm:
  grasp_orientation_rad: [0, 0, 0]
  final_tool_offset_m: [0, 0, 0.01]
  transition_tool_offset_m: [0, 0, -0.1]
height_profiles:
  ground:
    stations: [LM6]
    enabled: true
    grasp_orientation_rad: [0, 0, 0]
    final_tool_offset_m: [0, 0, 0.04]
'''
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory)/'config.yaml';file.write_text(text,encoding='utf-8')
            _, result = tool.prepare(file, 'lm6', [0.5, 0, 0.1])
            self.assertAlmostEqual(result['poses_m_rad']['final'][2], 0.14)
            self.assertAlmostEqual(result['poses_m_rad']['transition'][2], 0.04)
            with self.assertRaisesRegex(ValueError, '恰好属于'):
                tool.prepare(file, 'LM20', [0.5, 0, 0.1])


if __name__ == '__main__':
    unittest.main(verbosity=2)
