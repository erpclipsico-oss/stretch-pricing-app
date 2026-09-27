# PythonAnywhere WSGI config.
#
# In the PythonAnywhere "Web" tab, open the WSGI configuration file it
# generated for you and replace its ENTIRE contents with this file's
# contents, after fixing the two placeholders marked below.

import sys

# ---- 1. EDIT THIS: your PythonAnywhere username, and the project folder name ----
project_home = '/home/YOURUSERNAME/stretch-pricing-app'
# -----------------------------------------------------------------------------

if project_home not in sys.path:
    sys.path.insert(0, project_home)

from app.app import create_app

application = create_app()
