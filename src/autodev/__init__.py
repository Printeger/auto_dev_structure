"""AutoDev's public package identity."""

from __future__ import annotations

__version__ = "3.0.0a1"

from autodev.control_plane import Command, CommandResult, ControlPlane
from autodev.run_controller import RunController, RunOutcome, RunRequest
from autodev.campaign import CampaignController, CampaignOutcome, CampaignRequest
from autodev.campaign_workspace import CampaignWorkspace
from autodev.engines.app_server import AppServerCodexEngine
from autodev.human import HumanInteraction
from autodev.quality import QualityDecision

__all__ = [
    "Command", "CommandResult", "ControlPlane", "RunController", "RunOutcome", "RunRequest",
    "CampaignController", "CampaignOutcome", "CampaignRequest", "CampaignWorkspace",
    "HumanInteraction", "AppServerCodexEngine", "QualityDecision", "__version__",
]
