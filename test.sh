#!/usr/bin/env bash

# Usage: `tox -v`
#
# Or, standalone:
# ```
# source .tox/testenv/bin/activate
# ./test.sh
# ```
#
# FIXME: This test utility is a stopgap.
# If you are willing, please replace it with something more feature-rich, e.g. https://docs.python.org/3/library/unittest.html.

set -u

# Setup

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE"

assert_equals() {
	actual="$1"
	expected="$2"
	message="$3"

	if [[ "$actual" == "$expected" ]]; then
		echo "... assertion passed: $message"
	else
		echo -e "Assertion failed:"
		diff -u --color=always <(echo "${expected}") <(echo "${actual}")
		false
	fi
}

# Test: Fixture generates expected output

out=$(python statsv.py --dry-run --log-level DEBUG --workers 1 --kafka-fixture test/fixture_in.txt 2>&1)
# - strip variable BASE
# - strip variable datetime
# - strip line number
out=$(echo "${out//$BASE//statsv}" | sed 's/^[0-9-]* [0-9:,]* //' | sed 's/ line [0-9-]*/ line 0/')

assert_equals "$out" "$(cat test/fixture_out.txt)" "fixture_out.txt"
