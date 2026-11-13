# Source this file to point boto3 at a local AWS Emulator (github.com/aws/aws-emulator).
#   source scripts/emulator-env.sh
# Start the emulator first (see the README). It serves HTTPS on 127.0.0.1:9297 with a
# self-signed certificate, and on every start writes a fresh CA, a config file and a pair of
# run-only keys to ~/.aws/emulator/<port>/. This file reads them from there.
# AWS_ENDPOINT_URL is the safety net: every AWS call goes to the emulator, never to real AWS,
# and novapay_agents.aws_env refuses to run unless it points at this machine.
export EMULATOR_PORT="${EMULATOR_PORT:-9297}"
_np_emu_dir="${EMULATOR_DIR:-$HOME/.aws/emulator/$EMULATOR_PORT}"
if [ ! -f "$_np_emu_dir/emulator_credentials" ]; then
  echo "emulator-env.sh: $_np_emu_dir/emulator_credentials not found; start the emulator first" >&2
  return 1 2>/dev/null || exit 1
fi
export AWS_ENDPOINT_URL="https://127.0.0.1:$EMULATOR_PORT"
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE
export AWS_SHARED_CREDENTIALS_FILE="$_np_emu_dir/emulator_credentials"
export AWS_CONFIG_FILE="$_np_emu_dir/config"
export AWS_CA_BUNDLE="$_np_emu_dir/ca.pem"
export AWS_EC2_METADATA_DISABLED=true
export AWS_REGION=ca-central-1
export AWS_DEFAULT_REGION=ca-central-1
