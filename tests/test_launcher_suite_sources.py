from scripts.launcher_agent import required_test_repositories
import pytest

def test_homepage_suite_includes_brand_and_portal_dependencies():
    assert required_test_repositories(['lifetech'])=={'.','BmoreMedTech','OrgPortal'}
    assert required_test_repositories(['web--component','org-api'])=={'.','OrgPortal'}
    assert required_test_repositories(['identity','usajobs'])=={'.','PIdP','CodeCollective'}
    assert required_test_repositories(['lifetech'],{'lifetech':{'status':'reused'}})=={'.'}
    with pytest.raises(ValueError,match='Unknown suite'):
        required_test_repositories(['../arbitrary'])
