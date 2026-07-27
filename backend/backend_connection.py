import requests


class BackendConnection:
    """
    Unified HTTP backend connection.
    - Universal router endpoint (/command)
    - Direct endpoint calls (/anything)
    - Tracks last packet + last result for PacketInspectorV2
    """

    def __init__(self):
        self.base = "http://127.0.0.1:5000"

        # PacketInspectorV2 tracking
        self.last_packet = None
        self.last_result = None

    # ---------------------------------------------------------
    # UNIVERSAL ROUTER (/command)
    # ---------------------------------------------------------
    def send(self, envelope: dict) -> dict:
        """
        Send a packet to the universal backend router.
        Always returns a unified result dict.
        """
        self.last_packet = envelope

        try:
            resp = requests.post(
                f"{self.base}/command",
                json=envelope,
                timeout=10
            )
            resp.raise_for_status()

            result = resp.json()
            self.last_result = result
            return result

        except Exception as e:
            error_result = {
                "status": "error",
                "message": "Backend unreachable",
                "detail": str(e)
            }
            self.last_result = error_result
            return error_result

    # ---------------------------------------------------------
    # DIRECT ENDPOINT SENDER
    # ---------------------------------------------------------
    def send_to(self, endpoint: str, envelope: dict) -> dict:
        """
        Send a packet directly to a backend endpoint.
        Required by Backend Test Panel.
        """
        self.last_packet = envelope

        try:
            resp = requests.post(
                f"{self.base}{endpoint}",
                json=envelope,
                timeout=10
            )
            resp.raise_for_status()

            result = resp.json()
            self.last_result = result
            return result

        except Exception as e:
            error_result = {
                "status": "error",
                "message": f"Backend unreachable at {endpoint}",
                "detail": str(e)
            }
            self.last_result = error_result
            return error_result
