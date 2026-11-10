"""Feishu channel integration.

Importing this package registers the feishu ``ChannelTypeSpec`` (side-effect of
importing ``spec``), so any ``import agent_platform.channels.feishu`` makes
``get_channel_spec("feishu")`` resolvable.
"""

from agent_platform.channels.feishu.spec import FEISHU_SPEC  # noqa: F401
