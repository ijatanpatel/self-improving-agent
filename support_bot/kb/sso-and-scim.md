# Single sign-on (SSO) and SCIM

## SAML single sign-on
SAML 2.0 single sign-on is available on the Business and Enterprise plans. Supported identity providers include Okta, Microsoft Entra ID (formerly Azure AD), Google Workspace and OneLogin. Set it up in Settings > Security > SSO.

## Enforcing SSO
When "Enforce SSO" is turned on, members can only sign in through your identity provider and their passwords stop working. The workspace Owner keeps a backup password login so you cannot be locked out if the identity provider has an outage.

## SCIM provisioning
SCIM 2.0 lets your identity provider create, update and deactivate Orbitask accounts automatically. SCIM is available on the Enterprise plan only and currently works with Okta and Microsoft Entra ID.
