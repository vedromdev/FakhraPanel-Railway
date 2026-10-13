"""Shared fixed values that more than one module must agree on."""

# Default Shadowsocks cipher. The DB schema DEFAULT, the config builder's
# fallback for NULL ss_method, and any client-link generator must all use it,
# otherwise server and link silently disagree on the cipher.
DEFAULT_SS_METHOD = "aes-256-gcm"
