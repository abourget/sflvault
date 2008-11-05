# -=- encoding: utf-8 -=-
#
# SFLvault - Secure networked password store and credentials manager.
#
# Copyright (C) 2008  Savoir-faire Linux inc.
#
# Author: Alexandre Bourget <alexandre.bourget@savoirfairelinux.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging


# ALL THE FOLLOWING IMPORTS MOVED TO vault.py:
import xmlrpclib
#import pylons
#from pylons import request
from base64 import b64decode, b64encode
from datetime import *
import time as stdtime
from decorator import decorator

from sflvault.lib.base import *
from sflvault.lib.vault import SFLvaultAccess
from sflvault.model import *

from sqlalchemy import sql, exceptions

log = logging.getLogger(__name__)


#
# Permissions decorators for XML-RPC calls
#

@decorator
def authenticated_user(func, self, *args, **kwargs):
    """Aborts if user isn't authenticated.

    Timeout check done in get_session.

    WARNING: authenticated_user READ the FIRST non-keyword argument
             (should be authtok)
    """
    s = get_session(args[0])

    if not s:
        return xmlrpclib.Fault(0, "Permission denied")

    self.sess = s

    return func(self, *args, **kwargs)

@decorator
def authenticated_admin(func, self, *args, **kwargs):
    """Aborts if user isn't admin.

    Check authenticated_user , everything written then applies here as well.
    """
    s = get_session(args[0])

    if not s:
        return xmlrpclib.Fault(0, "Permission denied")
    if not s['userobj'].is_admin:
        return xmlrpclib.Fault(0, "Permission denied, admin priv. required")

    self.sess = s

    return func(self, *args, **kwargs)



##
## See: http://wiki.pylonshq.com/display/pylonsdocs/Using+the+XMLRPCController
##
class XmlrpcController(XMLRPCController):
    """This controller is required to call model.Session.remove()
    after each call, otherwise, junk remains in the SQLAlchemy caches."""

    allow_none = True # Enable marshalling of None values through XMLRPC.
    
    def __call__(self, environ, start_response):
        """Invoke the Controller"""
        # WSGIController.__call__ dispatches to the Controller method
        # the request is routed to. This routing information is
        # available in environ['pylons.routes_dict']
        
        self.vault = SFLvaultAccess()
        
        self.vault.setup_timeout = config['sflvault.vault.setup_timeout']

        try:
            return XMLRPCController.__call__(self, environ, start_response)
        finally:
            model.meta.Session.remove()
    
    def sflvault_login(self, username):
        # Return 'cryptok', encrypted with pubkey.
        # Save decoded version to user's db field.
        try:
            u = query(User).filter_by(username=username).one()
        except Exception, e:
            return vaultMsg(False, "User unknown: %s" % e.message )
        
        # TODO: implement throttling ?

        rnd = randfunc(32)
        # 15 seconds to complete login/authenticate round-trip.
        u.logging_timeout = datetime.now() + timedelta(0, 15)
        u.logging_token = b64encode(rnd)

        meta.Session.flush()
        meta.Session.commit()

        e = u.elgamal()
        cryptok = serial_elgamal_msg(e.encrypt(rnd, randfunc(32)))
        return vaultMsg(True, 'Authenticate please', {'cryptok': cryptok})

    def sflvault_authenticate(self, username, cryptok):
        """Receive the *decrypted* cryptok, b64 encoded"""

        u = None
        try:
            u = query(User).filter_by(username=username).one()
        except:
            return vaultMsg(False, 'Invalid user')

        if u.logging_timeout < datetime.now():
            return vaultMsg(False, 'Login token expired. Now: %s Timeout: %s' % (datetime.now(), u.logging_timeout))

        # str() necessary, to convert buffer to string.
        if cryptok != str(u.logging_token):
            return vaultMsg(False, 'Authentication failed')
        else:
            newtok = b64encode(randfunc(32))
            set_session(newtok, {'username': username,
                                 'timeout': datetime.now() + timedelta(0, int(config['sflvault.vault.session_timeout'])),
                                 'userobj': u,
                                 'user_id': u.id
                                 })

            return vaultMsg(True, 'Authentication successful', {'authtok': newtok})


    def sflvault_setup(self, username, pubkey):

        # First, remove ALL users that have waiting_setup expired, where
        # waiting_setup isn't NULL.
        #meta.Session.delete(query(User).filter(User.waiting_setup != None).filter(User.waiting_setup < datetime.now()))
        #raise RuntimeError
        cnt = query(User).count()
        
        u = query(User).filter_by(username=username).first()


        if (cnt):
            if (not u):
                return vaultMsg(False, 'No such user %s' % username)
        
            if (u.setup_expired()):
                return vaultMsg(False, 'Setup expired for user %s' % username)

        # Ok, let's save the things and reset waiting_setup.
        u.waiting_setup = None
        u.pubkey = pubkey

        meta.Session.commit()

        return vaultMsg(True, 'User setup complete for %s' % username)


    @authenticated_user
    def sflvault_get_service(self, authtok, service_id):
        self.vault.myself_id = self.sess['user_id']
        return self.vault.get_service(service_id)

    @authenticated_user
    def sflvault_get_service_tree(self, authtok, service_id):
        self.vault.myself_id = self.sess['user_id']
        return self.vault.get_service_tree(service_id)

    @authenticated_user
    def sflvault_put_service(self, authtok, service_id, data):
        self.vault.myself_id = self.sess['user_id']
        return self.vault.put_service(service_id, data)

    @authenticated_user
    def sflvault_show(self, authtok, service_id):
        self.vault.myself_id = self.sess['user_id']
        return self.vault.show(service_id)

    @authenticated_user
    def sflvault_search(self, authtok, search_query, verbose=False):
        return self.vault.search(search_query, verbose)

    @authenticated_admin
    def sflvault_adduser(self, authtok, username, is_admin):
        return self.vault.add_user(username, is_admin)

    @authenticated_admin
    def sflvault_grant(self, authtok, user, group_ids):
        self.vault.myself_id = self.sess['user_id']
        return self.vault.grant(user, group_ids)

    @authenticated_admin
    def sflvault_grantupdate(self, authtok, user, ciphers):
        return self.vault.grant_update(user, ciphers)

    @authenticated_admin
    def sflvault_revoke(self, authtok, user, group_ids):
        return self.vault.revoke(user, group_ids)
        
    @authenticated_admin
    def sflvault_analyze(self, authtok, user):
        return self.vault.analyze(user)

    @authenticated_user
    def sflvault_addmachine(self, authtok, customer_id, name, fqdn, ip,
                            location, notes):
        return self.vault.add_machine(customer_id, name, fqdn, ip,
                                      location, notes)

    @authenticated_user
    def sflvault_addservice(self, authtok, machine_id, parent_service_id, url,
                            group_ids, secret, notes):
        self.vault.myself_id = self.sess['user_id']
        return self.vault.add_service(machine_id, parent_service_id, url,
                                      group_ids, secret, notes)
        
    @authenticated_user
    def sflvault_addcustomer(self, authtok, customer_name):
        self.vault.myself_username = self.sess['username']
        return self.vault.add_customer(customer_name)

    @authenticated_admin
    def sflvault_deluser(self, authtok, user):
        return self.vault.del_user(user)

    @authenticated_admin
    def sflvault_delcustomer(self, authtok, customer_id):
        return self.vault.del_customer(customer_id)

    @authenticated_admin
    def sflvault_delmachine(self, authtok, machine_id):
        return self.vault.del_machine(machine_id)

    @authenticated_admin
    def sflvault_delservice(self, authtok, service_id):
        return self.vault.del_service(service_id)

    @authenticated_user
    def sflvault_listcustomers(self, authtok):
        return self.vault.list_customers()

    @authenticated_admin
    def sflvault_addgroup(self, authtok, group_name):
        return self.vault.add_group(group_name)

    @authenticated_user
    def sflvault_listgroups(self, authtok):
        return self.vault.list_groups()

    @authenticated_user
    def sflvault_listmachines(self, authtok, customer_id=None):
        return self.vault.list_machines(customer_id)

    @authenticated_user
    def sflvault_listusers(self, authtok):
        return self.vault.list_users()

    @authenticated_user
    def sflvault_chgservicepasswd(self, authtok, service_id, newsecret):
        self.vault.myself_id = self.sess['user_id']        
        return self.vault.chg_service_passwd(service_id, newsecret)
