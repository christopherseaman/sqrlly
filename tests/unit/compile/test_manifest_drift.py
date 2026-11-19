"""Compare branch identities when a repeating node resumes."""
import pytest

from sqrlly.compile._manifest import manifest_drift


class TestManifestDrift:
    @pytest.mark.parametrize(
        "prior,new,expected",
        [
            ({"cfan::a", "cfan::b", "cfan::c"},
             {"cfan::a", "cfan::b", "cfan::c"}, set()),          # stable → no drift
            ({"cfan::a", "cfan::b"},
             {"cfan::a", "cfan::b", "cfan::c"}, set()),          # additive superset → no drift
            ({"cfan::r1a", "cfan::r1b", "cfan::r1c"},
             {"cfan::r2a", "cfan::r2b", "cfan::r2c"},
             {"cfan::r1a", "cfan::r1b", "cfan::r1c"}),           # full drift → all prior
            ({"cfan::a", "cfan::b", "cfan::c"},
             {"cfan::a", "cfan::c"}, {"cfan::b"}),               # one dropped
        ],
    )
    def test_drift_is_prior_minus_new(self, prior, new, expected):
        assert manifest_drift(prior, new) == expected
