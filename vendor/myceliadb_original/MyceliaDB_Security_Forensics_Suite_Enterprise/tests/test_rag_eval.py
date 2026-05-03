import unittest
from mycelia_security_forensics.rag_eval import RagCase, evaluate_rag_response


class RagEvalTests(unittest.TestCase):
    def test_pass(self):
        case = RagCase(id="x", question="q")
        ev = evaluate_rag_response(case, {"status":"ok","answer":"safe","sources":[{"id":"s"}],"retrieval_backend":"mycelia:opencl-vram"})
        self.assertEqual(ev.status, "pass")

    def test_secret_fail(self):
        case = RagCase(id="x", question="q")
        ev = evaluate_rag_response(case, {"status":"ok","answer":"local_transport.token is abc","sources":[{"id":"s"}],"retrieval_backend":"mycelia:opencl-vram"})
        self.assertEqual(ev.status, "fail")


if __name__ == "__main__":
    unittest.main()
