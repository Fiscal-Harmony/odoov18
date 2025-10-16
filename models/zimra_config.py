# zimra_fiscal/models/zimra_config.py
# -*- coding: utf-8 -*-
from email.policy import default

from odoo import models, fields, api
from odoo.exceptions import ValidationError
import requests
import json
import logging
import hmac
import hashlib
import base64
from datetime import datetime, time
import time

_logger = logging.getLogger(__name__)


class ZimraConfig(models.Model):
    _name = 'zimra.config'
    _description = 'ZIMRA Configuration'
    _rec_name = 'name'

    name = fields.Char('Configuration Name', required=True)
    api_url = fields.Char('API URL', required=True,
                          default='https://api.fiscalharmony.co.zw/api')
    api_key = fields.Char('API Key', required=True)
    api_secret = fields.Char('API Secret', required=True)
    company_id = fields.Many2one('res.company', 'Company', required=True,
                                 default=lambda self: self.env.company)
    active = fields.Boolean('Active', default=True)
    userId = fields.Integer('Fiscal Harmony User ID', default=0)

    # Additional Configuration
    timeout = fields.Integer('Request Timeout (seconds)', default=30)
    auto_fiscalize = fields.Boolean('Auto Fiscalize', default=True,
                                    help='Automatically fiscalize POS orders when paid')
    retry_count = fields.Integer('Retry Count', default=8,
                                 help='Number of times to retry failed requests')
    # In your zimra_config model
   # auto_fiscalize_invoices = fields.Boolean('Auto-fiscalize Invoices on Post', default=False)
    #auto_fiscalize_on_payment = fields.Boolean('Auto-fiscalize on Payment', default=False)

    # Tax and Currency Mappings
    tax_mapping_ids = fields.One2many('zimra.tax.mapping', 'config_id', 'Tax Mappings')
    currency_mapping_ids = fields.One2many('zimra.currency.mapping', 'config_id', 'Currency Mappings')

    # Statistics
    total_sent = fields.Integer('Total Sent', compute='_compute_statistics')
    total_fiscalized = fields.Integer('Total Fiscalized', compute='_compute_statistics')
    total_failed = fields.Integer('Total Failed', compute='_compute_statistics')

    # Last successful request tracking
    last_successful_request = fields.Datetime('Last Successful Request')
    device_taxes_synced = fields.Boolean('Device Taxes Synced')
    last_tax_sync = fields.Datetime('Last Tax Sync')

    @api.depends('company_id')
    def _compute_statistics(self):
        for record in self:
            domain = [('company_id', '=', record.company_id.id)]

            record.total_sent = self.env['pos.order'].search_count(
                domain + [('zimra_status', 'in', ['sent', 'fiscalized'])]
            )
            record.total_fiscalized = self.env['pos.order'].search_count(
                domain + [('zimra_status', '=', 'fiscalized')]
            )
            record.total_failed = self.env['pos.order'].search_count(
                domain + [('zimra_status', '=', 'failed')]
            )

    @api.constrains('api_key')
    def _check_api_key(self):
        for record in self:
            if record.api_key and len(record.api_key) < 10:
                raise ValidationError('API Key must be at least 10 characters long')

    @api.constrains('api_url')
    def _check_api_url(self):
        for record in self:
            if not record.api_url.startswith(('http://', 'https://')):
                raise ValidationError('API URL must start with http:// or https://')

    def __encode_data(self, data: dict) -> str:
        """Encodes the given data as a valid JSON string for transmitting.

        Args:
            data (dict): The data to be processed.

        Returns:
            str: The JSON representation of the given data.
        """
        return json.dumps(data, separators=(",", ":"), sort_keys=True)

    def __get_request_url(self, route: str) -> str:
        """Constructs and returns the route for the API request.

        Args:
            route (str): The path for the request.

        Returns:
            str: The constructed URL.
        """
        if route.startswith("/"):
            return self.api_url.rstrip('/') + route
        return f"{self.api_url.rstrip('/')}/{route}"

    def __get_headers(self, api_key: str | None = None) -> dict[str, str]:
        """Generate the headers based on the either the stored or provided API details.

        Args:
            api_key (str | None, optional): The API Key to use instead of the stored value.\
                Defaults to None.

        Returns:
            dict[str,str]: The headers in dictionary format."""

        api_key: str = self.api_key if api_key is None else api_key
        headers = {
            "X-Api-Key": api_key,
            "X-Application": "FH_Quickbooks",
            "X-App-Station": "",
            "Content-Type": "application/json"
        }

        return headers

    def __get_signed_headers(self, payload: str) -> dict:
        """Generate the headers with a signature based on the payload.

        Args:
            payload (str): The JSON encoded body of the request.

        Returns:
            dict: The headers in a dictionary format, including the signature.
        """
        headers = self.__get_headers()
        signature = self.__sign_payload(payload)


        headers["X-Api-Signature"] = signature


        return headers

    def __get_authheaders(self, api_key: str | None = None) -> dict[str, str]:
        """Generate the headers based on the either the stored or provided API details.

        Args:
            api_key (str | None, optional): The API Key to use instead of the stored value.\
                Defaults to None.

        Returns:
            dict[str,str]: The headers in dictionary format."""

        api_key: str = self.api_key if api_key is None else api_key
        headers = {
            "X-Api-Key": api_key,
            # "X-Application": "FH_Quickbooks",
            # "X-App-Station": "ERPNext",
        }

        return headers

    def __update_last_successful_request(self):
        """Update the timestamp of the last successful request."""
        self.last_successful_request = fields.Datetime.now()

    def __update_last_taxsync(self):
        self.last_tax_sync = fields.Datetime.now()

    def __istax_synced(self):
        self.device_taxes_synced = 1

    def __log_request(self, log_data: dict):
        """Log request data for debugging and monitoring.

        Args:
            log_data (dict): The log data to record.
        """
        log_message = f"ZIMRA API Request - Status: {log_data.get('status', 'Unknown')}"
        if log_data.get('error_details'):
            log_message += f" - Error: {log_data['error_details']}"

        _logger.info(f"{log_message} - URL: {log_data.get('request_url', 'N/A')}")

        if log_data.get('response'):
            _logger.debug(f"Response: {log_data['response']}")

    def __make_request(self, route: str) -> requests.Response:
        """Generates and processes a standard GET request to the Fiscal Harmony API.

        Args:
            route (str): The route to request against.

        Returns:
            requests.Response: The response from the Fiscal Harmony platform.
        """
        request_url = self.__get_request_url(route)
        headers = self.__get_authheaders()
        _logger.info(f"Request Headers: {headers}")

        log_data = {
            "request_url": request_url,
            "method": "GET",
            "timestamp": datetime.now().isoformat()
        }

        try:
            response = requests.get(
                request_url,
                headers=headers,
                timeout=self.timeout,
            )

            log_data["response_status_code"] = response.status_code

            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                try:
                    response_json = response.json()
                    log_data["response"] = json.dumps(response_json, indent=2)
                except json.JSONDecodeError:
                    log_data["response"] = "Invalid JSON response"
            else:
                log_data["response"] = f"Non-JSON response (Content-Type: {content_type})"

            response.raise_for_status()
            log_data["status"] = "Success"
            self.__update_last_successful_request()

        except requests.exceptions.Timeout:
            log_data["status"] = "Failure"
            log_data["error_details"] = f"Connection timed out after {self.timeout} seconds"
            log_data["response_status_code"] = 500
            self.__log_request(log_data)
            raise ValidationError("The connection timed out.")

        except requests.exceptions.ConnectionError:
            log_data["status"] = "Failure"
            log_data["error_details"] = "Connection error"
            log_data["response_status_code"] = 500
            self.__log_request(log_data)
            raise ValidationError(
                "Unable to connect to Fiscal Harmony API. Please check your internet connection and API URL.")

        except requests.exceptions.HTTPError:
            log_data["error_details"] = response.reason
            if response.status_code == 401:
                log_data["status"] = "Unauthorised"
                error_message = "Unauthorized access. Please check your API credentials."
            else:
                log_data["status"] = "Failure"
                error_message = f"HTTP Error {response.status_code}: {response.reason}"

            self.__log_request(log_data)
            raise ValidationError(error_message)

        except Exception as e:
            log_data["status"] = "Failure"
            log_data["error_details"] = str(e)
            log_data["response_status_code"] = 500
            self.__log_request(log_data)
            raise ValidationError(f"Request error: {str(e)}")

        self.__log_request(log_data)
        return response

    def __sign_payload(self, payload: str) -> str:
        """Generate the signature for the given `payload`.

        Args:
            payload (str): The payload to be signed.

        Returns:
            str: The generated signature."""

        hasher = hmac.new(
            self.api_secret.encode("utf-8"),
            msg=payload.encode("utf-8"),
            digestmod=hashlib.sha256,
        )
        signature = base64.b64encode(hasher.digest()).decode("utf-8")

        return signature

    def __make_signed_request(self, route: str, data: dict | str| list, method: str = 'POST') -> requests.Response:
        """Generates and processes a signed request to the Fiscal Harmony API.

        This implementation matches the JavaScript signature generation:
        - Only POST, PUT, PATCH methods use the body for signing
        - The raw body string is used directly for HMAC-SHA256 signature
        - Empty body for GET requests

        Args:
            route (str): The route to request against.
            data (dict): The data to be sent in the request body.
            method (str): HTTP method (POST, PUT, etc.). Defaults to 'POST'.

        Returns:
            requests.Response: The response from the Fiscal Harmony platform.
        """
        request_url = self.__get_request_url(route)

        # Initialize body based on method (matching JavaScript logic)
        body = ""
        if method.upper() in ["POST", "PUT", "PATCH"]:
            # Handle both dict and string inputs
            if isinstance(data, dict):
                # Convert dict to compact JSON for signing
                _logger.info("Converting dict to Json %s",data)
                body = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
            elif isinstance(data,list):
                 # Convert list to compact JSON for signing
                 _logger.info("Converting list to Json %s", data)
                 body = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
                 _logger.info(body)

            else:
                # Parse formatted JSON string and re-serialize as compact JSON
                try:
                    parsed_data = json.loads(data)
                    body = json.dumps(parsed_data, separators=(',', ':'), ensure_ascii=False)
                    _logger.info("successfully loaded json %s",body)
                except json.JSONDecodeError:
                    # If it's not valid JSON, use as-is
                    _logger.info("no need to  format to json using as is %s",data)
                    body = data

        # Generate headers with signature based on the body
        headers = self.__get_signed_headers(body)
        _logger.info(f"Request URL: {request_url}")
        _logger.info(f"Request Headers: {headers}")

        log_data = {
            "request_url": request_url,
            "method": method,
            "body": body,
            "timestamp": datetime.now().isoformat()
        }

        _logger.info("sending this object for fiscalisation %s",log_data)

        try:
            if method.upper() == 'POST':
                response = requests.post(
                    request_url,
                    data=body,
                    headers=headers,
                    timeout=self.timeout,
                )
            elif method.upper() == 'PUT':
                response = requests.put(
                    request_url,
                    data=body,
                    headers=headers,
                    timeout=self.timeout,
                )
            elif method.upper() == 'PATCH':
                response = requests.patch(
                    request_url,
                    data=body,
                    headers=headers,
                    timeout=self.timeout,
                )
            else:
                raise ValidationError(f"Unsupported HTTP method: {method}")

            log_data["response_status_code"] = response.status_code

            try:
                response_json = response.text
                _logger.info("response plain %s",response.text)
                log_data["response"] = json.dumps(response_json, indent=2)
            except json.JSONDecodeError:
                log_data["response"] = response.text

            response.raise_for_status()
            log_data["status"] = "Success"
            self.__update_last_successful_request()

        except requests.exceptions.Timeout:
            log_data["status"] = "Failure"
            log_data["error_details"] = f"Connection timed out after {self.timeout} seconds"
            log_data["response_status_code"] = 500
            self.__log_request(log_data)
            raise ValidationError("The connection timed out.")

        except requests.exceptions.ConnectionError:
            log_data["status"] = "Failure"
            log_data["error_details"] = "Connection error"
            log_data["response_status_code"] = 500
            self.__log_request(log_data)
            raise ValidationError("Unable to connect to ZIMRA API. Please check your internet connection and API URL.")

        except requests.exceptions.HTTPError:
            log_data["error_details"] = response.reason
            if response.status_code == 401:
                log_data["status"] = "Unauthorised"
                error_message = "Unauthorized access. Please check your API credentials."
            else:
                log_data["status"] = "Failure"
                error_message = f"HTTP Error {response.status_code}: {response.reason}"

            self.__log_request(log_data)
            raise ValidationError(error_message)

        except Exception as e:
            log_data["status"] = "Failure"
            log_data["error_details"] = str(e)
            log_data["response_status_code"] = 500
            self.__log_request(log_data)
            raise ValidationError(f"Request error: {str(e)}")

        self.__log_request(log_data)
        return response
    def save_taxmapping(self, mapping):
        self.ensure_one()
        if not mapping.odoo_tax_id or not mapping.zimra_tax_code:
            return

        payload = {
            "UserId": self.userId,
            "TaxCode": mapping.odoo_tax_id.name,
            "TaxName": f"{mapping.odoo_tax_id.name} ({mapping.odoo_tax_id.amount}%)",
            "DestinationTaxId": int(mapping.zimra_tax_code),

        }
        _logger.info(payload)

        route = "/taxmapping"
        headers = self.__get_signed_headers(json.dumps(payload))
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload)
        url = self.__get_request_url(route)
        response = requests.post(url, headers=headers, data=data, timeout=self.timeout)

        if response.status_code in [200, 201]:
            # mapping.synced_to_zimra = True
            _logger.info(response)
            return response.json()
        else:
            raise ValidationError(f"ZIMRA returned error: {response.text}")

        # if mapping.synced_to_zimra and mapping.zimra_tax_code:
        # Update (PUT)
        # url = self.__get_request_url(f"{route}/{mapping.zimra_tax_code}")
        # response = requests.put(url, headers=headers, data=data, timeout=self.timeout)

    # else:
    # Create (POST)
    # url = self.__get_request_url(route)
    # response = requests.post(url, headers=headers, data=data, timeout=self.timeout)
    def save_currencymapping(self, mapping):
        self.ensure_one()
        if not mapping.odoo_currency_id or not mapping.zimra_currency_code:
            return

        payload = {
            "UserId": self.userId,
            "SourceCurrency": mapping.odoo_currency_id.name,
            "DestinationCurrency": mapping.zimra_currency_code
        }
        _logger.info(payload)

        route = "/currencymapping"
        headers = self.__get_signed_headers(json.dumps(payload))
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload)
        url = self.__get_request_url(route)
        response = requests.post(url, headers=headers, data=data, timeout=self.timeout)

        if response.status_code in [200, 201]:
            # mapping.synced_to_zimra = True
            _logger.info(response)
            return response.json()
        else:
            raise ValidationError(f"ZIMRA returned error: {response.text}")

    def test_connection(self):
        """Test connection to FISCAL HARMONY API"""
        self.ensure_one()
        try:
            response = self.__make_request("/profile")

            if response.status_code == 200:
                data = response.json()
                user_id = data.get("Id", "Unknown")
                company = data.get("FullName","Unknown")
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Connection Successful',
                        'message': f'Successfully connected to Fiscal Harmony API. UserId  {user_id}',
                        'type': 'success',
                        'sticky': False,
                    }
                }
            else:
                raise ValidationError(f"Connection failed with status {response.status_code}")

        except Exception as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Connection Failed',
                    'message': str(e),
                    'type': 'danger',
                    'sticky': True,
                }
            }

    def send_fiscal_data(self, data, route: str = "/invoice") -> dict:
        """Send fiscal data to ZIMRA API with signature."""

        self.ensure_one()

        # Normalize only for checking Reference
        if isinstance(data, str):
            try:
                preview = json.loads(data)
            except json.JSONDecodeError:
                _logger.error("Invalid JSON passed to send_fiscal_data")
                return {"status": "error", "reason": "invalid JSON"}
        elif isinstance(data, dict):
            preview = data
        else:
            _logger.error(f"Unsupported type for data: {type(data)}")
            return {"status": "error", "reason": "unsupported data type"}

        ref = preview.get("Reference", "")
        if isinstance(ref, str) and ref.startswith("Shop/"):
            _logger.info(f"Skipping fiscalisation for reference starting with 'Shop/': {ref}")
            return {"status": "skipped", "reason": "Shop reference"}

        try:
            # Send data as-is (no serialization)
            response = self.__make_signed_request(route, data)

            _logger.info(" Transaction response string: %s", response.text.strip())
            parsed = response.text.strip()
            fiscalstatus = [parsed]
            _logger.info("StatusString %s", fiscalstatus)

            time.sleep(6)
            response = self.check_fiscalisation_status(fiscalstatus, "/status")

            return response
        except Exception as e:
            _logger.error(f"Failed to send fiscal data: {str(e)}")
            raise

    def check_fiscalisation_status(self, data: list, route: str = "/status") -> dict:
        """Send fiscal data to ZIMRA API with signature.

        Args:
            data (dict): Invoice guuid to check status for .
            route (str): The API route to send to. Defaults to "/status".

        Returns:
            dict: The response from the API.
        """
        self.ensure_one()

        try:
            response = self.__make_signed_request(route, data)

            _logger.info(" Transaction response: %s", response.json())

            return response.json()
        except Exception as e:
            _logger.error(f"Failed to check status: {str(e)}")
            raise

    def retry_failed_request(self, route: str, data: dict = None, method: str = 'GET') -> dict:
        """Retry a failed request with exponential backoff.

        Args:
            route (str): The API route.
            data (dict, optional): Request data for signed requests.
            method (str): HTTP method. Defaults to 'GET'.

        Returns:
            dict: The response from the API.
        """
        import time

        for attempt in range(self.retry_count):
            try:
                if data:
                    response = self.__make_signed_request(route, data, method)
                else:
                    response = self.__make_request(route)
                return response.json()
            except Exception as e:
                if attempt == self.retry_count - 1:  # Last attempt
                    raise
                # Exponential backoff
                time.sleep(2 ** attempt)

        raise ValidationError("Max retry attempts reached")

    def action_view_pos_orders(self):
        """View POS orders for this configuration"""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'POS Orders',
            'res_model': 'pos.order',
            'view_mode': 'tree,form',
            'domain': [('company_id', '=', self.company_id.id)],
            'context': {'default_company_id': self.company_id.id}
        }

    def action_view_failed_orders(self):
        """View failed POS orders"""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Failed Orders',
            'res_model': 'pos.order',
            'view_mode': 'tree,form',
            'domain': [
                ('company_id', '=', self.company_id.id),
                ('zimra_status', '=', 'failed')
            ],
            'context': {'default_company_id': self.company_id.id}
        }

    def get_device_taxes(self):
        """Fetch taxes from the device endpoint and return them.

        Returns:
            dict: Device information including taxes, or None if failed
        """
        self.ensure_one()
        try:
            response = self.__make_request("/fiscaldevice")
            if response.status_code == 200:
                device_data = response.json()
                self.__istax_synced()
                self.__update_last_taxsync()

                _logger.info(device_data)

                # Get and parse the CurrentConfig JSON string
                current_config_str = device_data.get("CurrentConfig", "{}")
                current_config = json.loads(current_config_str)

                # Extract taxID and taxName only
                applicable_taxes = current_config.get("applicableTaxes", [])
                _logger.info(applicable_taxes)
                simplified_taxes = [
                    {"taxID": tax.get("taxID"), "taxName": tax.get("taxName")}
                    for tax in applicable_taxes
                    if tax.get("taxID") is not None and tax.get("taxName") is not None
                ]
                _logger.info("applicable:%s", simplified_taxes)

                return simplified_taxes

            else:
                _logger.error(f"Failed to fetch device taxes: Status {response.status_code}")
                return None
        except Exception as e:
            _logger.error(f"Error fetching device taxes: {str(e)}")
            return None

    def download_pdf(self, fiscalpdf: str):
        """Download and show Fiscal PDF in POS modal."""
        self.ensure_one()

        response = self.__make_request(f"/download/{fiscalpdf}")

        if response.status_code == 200:
            pdf_data = base64.b64encode(response.content).decode()
            return  pdf_data
        else:
            return response.status_code

    def sync_device_taxes(self):
        """Sync taxes from device endpoint to local tax mappings.

        This method fetches taxes from the device and updates/creates
        local tax mappings accordingly.

        Returns:
            dict: Action result for notification
        """
        self.ensure_one()
        try:
            device_data = self.get_device_taxes()
            if not device_data:
                raise ValidationError("Failed to fetch device data")

            # Extract taxes from device response
            taxes = device_data
            if not taxes:
                taxes = []

            if not taxes:
                raise ValidationError("No taxes found in device response")

            # Clear existing tax mappings for this config
            self.tax_mapping_ids.unlink()

            # Get the tax mapping model to use its normalization method
            TaxMapping = self.env['zimra.tax.mapping']

            # Create new tax mappings from device response
            tax_mappings = []
            _logger.info("Taxes Pulled are %s", taxes)

            # Get the tax mapping model to use its normalization method
            TaxMapping = self.env['zimra.tax.mapping']

            for tax_data in taxes:
                tax_name = tax_data.get('taxName', '')

                # Use the model's normalization method for consistency
                tax_type = TaxMapping.normalize_tax_type(tax_name)

                # Extract tax rate properly - it should come from taxPercent, not default to 0.0
                tax_rate = tax_data.get('taxPercent', 0.0)
                if tax_rate is None:  # Handle None values for exempt taxes
                    tax_rate = 0.0

                tax_mapping = {
                    'config_id': self.id,
                    'zimra_tax_code': tax_data.get('taxID', ''),
                    'zimra_tax_name': tax_name,
                    'zimra_tax_rate': tax_rate,
                    'zimra_tax_type': tax_type,
                }
                tax_mappings.append((0, 0, tax_mapping))
                _logger.info("Creating tax mapping: %s -> %s", tax_name, tax_type)

            # Update the tax mappings
            if tax_mappings:
                self.write({'tax_mapping_ids': tax_mappings})

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Taxes Synced',
                    'message': f'Successfully synced {len(taxes)} taxes from device',
                    'type': 'success',
                    'sticky': False,
                }
            }

        except ValidationError as ve:
            _logger.error(f"Validation error syncing device taxes: {str(ve)}")
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Sync Failed',
                    'message': f'Validation error: {str(ve)}',
                    'type': 'danger',
                    'sticky': True,
                }
            }
        except Exception as e:
            _logger.error(f"Failed to sync device taxes: {str(e)}")
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Sync Failed',
                    'message': f'Failed to sync taxes: {str(e)}',
                    'type': 'danger',
                    'sticky': True,
                }
            }
    def get_available_taxes(self):
        """Get available taxes for this device configuration.

        Returns:
            list: List of available taxes from device or local cache
        """
        self.ensure_one()

        # First try to get from device
        device_data = self.get_device_taxes()
        if device_data and device_data.get('taxes'):
            return device_data['taxes']

        # Fallback to local tax mappings
        local_taxes = []
        for mapping in self.tax_mapping_ids:
            local_taxes.append({
                'code': mapping.zimra_tax_code,
                'name': mapping.zimra_tax_name,
                'rate': mapping.zimra_tax_rate,
                'type': mapping.zimra_tax_type,
            })

        return local_taxes

    def validate_tax_code(self, tax_code):
        """Validate if a tax code is available for this device.

        Args:
            tax_code (str): The tax code to validate

        Returns:
            bool: True if tax code is valid, False otherwise
        """
        self.ensure_one()
        available_taxes = self.get_available_taxes()

        for tax in available_taxes:
            if tax.get('code') == tax_code:
                return True

        return False

    def get_tax_rate_by_code(self, tax_code):
        """Get tax rate by tax code.

        Args:
            tax_code (str): The tax code

        Returns:
            float: Tax rate, or 0.0 if not found
        """
        self.ensure_one()
        available_taxes = self.get_available_taxes()

        for tax in available_taxes:
            if tax.get('code') == tax_code:
                return tax.get('rate', 0.0)

        return 0.0


    def cron_sync_device_taxes(self):
        """Cron job to periodically sync device taxes for all active configurations."""
        active_configs = self.search([('active', '=', True)])

        for config in active_configs:
            try:
                config.sync_device_taxes()
                _logger.info(f"Successfully synced taxes for config: {config.name}")
            except Exception as e:
                _logger.error(f"Failed to sync taxes for config {config.name}: {str(e)}")

    # Add this method to enhance your fiscal data sending
    def send_fiscal_data_with_validation(self, data: dict, route: str = "/fiscalize") -> dict:
        """Send fiscal data with tax validation against device taxes.

        Args:
            data (dict): The fiscal data to send
            route (str): The API route to send to

        Returns:
            dict: The response from the API
        """
        self.ensure_one()

        # Validate tax codes if present in data
        if 'items' in data:
            for item in data['items']:
                if 'tax_code' in item:
                    if not self.validate_tax_code(item['tax_code']):
                        raise ValidationError(
                            f"Invalid tax code '{item['tax_code']}' for device. "
                            f"Please sync device taxes first."
                        )

        # Send the data
        return self.send_fiscal_data(data, route)
