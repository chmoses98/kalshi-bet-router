# Sourced by deliver-wagers.yml and settle-wagers.yml (and by tests/test_clone_retry.py).
#
# clone_retry <git clone args...> <dest>
#
# Shallow-clones with BOUNDED retries and exponential backoff (2s, 4s, 8s between four attempts). The destination
# directory (the LAST argument) is removed before every attempt, so a half-written checkout from a reset connection
# can never be mistaken for a clone. Returns 0 on success, 1 after the fourth failure. It never exits the shell, so
# the caller's `if ! clone_retry ...; then failures=$((failures+1)); continue; fi` costs ONE destination its run and
# nothing else. It touches only the checkout directory: no push, no PR, no ledger write.
#
# CLONE_RETRY_SLEEP (default `sleep`) exists so tests can run without waiting; production never sets it.
clone_retry() {
  local attempt delay=2 sleeper="${CLONE_RETRY_SLEEP:-sleep}"
  for attempt in 1 2 3 4; do
    rm -rf "${@: -1}"
    if git clone --quiet --depth 1 "$@"; then return 0; fi
    [ "${attempt}" -lt 4 ] || break
    echo "  clone attempt ${attempt} failed; retrying in ${delay}s" >&2
    "${sleeper}" "${delay}"; delay=$((delay * 2))
  done
  return 1
}
