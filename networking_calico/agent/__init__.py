# Copyright 2015 Metaswitch Networks
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

# Workaround for https://github.com/kennethreitz/requests/issues/2870
import sys
import urllib3
import urllib3.exceptions as u3e
if sys.modules["urllib3"].exceptions is not sys.modules["urllib3.exceptions"]:
    sys.modules["requests.packages.urllib3"] = urllib3
    sys.modules["requests.packages.urllib3.exceptions"] = u3e
