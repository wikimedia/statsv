# statsv

## Local development

Requirements:
1. [pip](https://pip.pypa.io/en/stable/installing/). Linux: `apt-get install python3-pip`, macOS: Part of Python from Homebrew.
2. [tox](https://tox.readthedocs.io/en/latest/). Linux: `pip install tox`, macOS: `brew install tox`.

To run the tests, run `tox -v`.

### Local debugging

To ease local debugging, you can start Statsv locally without Kafka. Use the `--kafka-fixture` option to read lines of JSON from a static file.

```
$ tox -v
$ .tox/testenv/bin/python statsv.py --dry-run --kafka-fixture statsv_test_fixture.txt
```

## Deployment

Currently configured as a scap deploy.
```
ssh <deployment host>
cd /srv/deployment/statsv/statsv
git pull --rebase
scap deploy <task id or gerrit change id>
```
