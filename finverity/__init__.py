"""FinVerity: blockchain-anchored financial audit evidence (simulation framework)."""
import warnings

warnings.filterwarnings("ignore", category=UserWarning, module=r"eth_utils\..*|eth_abi")
warnings.filterwarnings("ignore", message=".*pkg_resources.*")

__version__ = "1.0.0"
