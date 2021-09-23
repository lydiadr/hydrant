from collections import OrderedDict
import requests
from urllib.parse import urlencode

from hydrant.config import FHIR_SERVER_URL


class PatientList(object):
    """Like a factory, used to build a list of patients via parser"""
    def __init__(self, parser, adapter):
        self.parser = parser
        self.adapter = adapter
        self.items = None

    def _parse(self):
        """Use parser and adapter, build up list of available patients"""
        self.items = []
        keys_seen = set()
        for row in self.parser.rows():
            # Adapter may define unique_key() - if defined and a previous
            # entry matches, skip over this "duplicate"
            if hasattr(self.adapter, 'unique_key'):
                key = self.adapter(row).unique_key()
                if key in keys_seen:
                    continue
                keys_seen.add(key)

            self.items.append(Patient.factory(row, self.adapter))

    def patients(self):
        if self.items is None:
            self._parse()

        return self.items


class Patient(object):
    """Minimal FHIR like Patient for parsing / uploading """
    def __init__(self, name=None, birthDate=None):
        self._fields = OrderedDict()
        if name:
            self._fields['name'] = name
        if birthDate:
            self._fields['birthDate'] = birthDate
        self._id = None

    def __repr__(self):
        if self._id is not None:
            return f"<Patient {self._id}>"
        elif 'name' in self._fields:
            return f"<Patient {self._fields['name']}"
        else:
            return f"<Patient>"

    def search_url(self):
        """Generate the request path search url for Patient

        NB - this method does NOT invoke a round trip ID lookup.
        Call self.id() beforehand to force a lookup.
        """
        if self._id:
            return f"Patient/{id}"

        # FHIR spec: 'birthDate'; HAPI search: 'birthdate'
        search_params = {
            "family": self._fields["name"]["family"],
            "given": self._fields["name"]["given"][0],
            "birthdate": self._fields["birthDate"],
        }
        return f"Patient/?{urlencode(search_params)}"

    def id(self):
        """Look up FHIR id or return None if not found"""
        if self._id is not None:
            return self._id

        # Round-trip to see if this represents a new or existing Patient
        if FHIR_SERVER_URL:
            headers = {'Cache-Control': 'no-cache'}
            response = requests.get('/'.join((FHIR_SERVER_URL, self.search_url())), headers=headers)
            response.raise_for_status()

            # extract Patient.id from bundle
            bundle = response.json()
            if bundle['total']:
                if bundle['total'] > 1:
                    raise RuntimeError(
                        "Found multiple matches, can't generate upsert"
                        f"for {self.search_url()}")
                assert bundle['entry'][0]['resource']['resourceType'] == 'Patient'
                self._id = bundle['entry'][0]['resource']['id']
        return self._id

    def as_fhir(self):
        results = {'resourceType': 'Patient'}
        results.update(self._fields)
        return results

    def as_upsert_entry(self):
        """Generate FHIR for inclusion in transaction bundle

        Transaction bundles need search and method details for
        FHIR server to perform requested task.

        :returns: JSON snippet to include in transaction bundle
        """
        results = {
            'resource': self.as_fhir(),
            'request': {
                'method': "PUT" if self.id() else "POST",
                'url': self.search_url()}}
        return results

    @classmethod
    def factory(cls, data, adapter_cls):
        """Using parser API, pull available Patient fields

        :param data: single `row` of data, from parsed file or db
        :param adapter_cls: class to be instantiated on `data` with
          accessor methods to obtain patient attributes from given
          format.

        :returns: populated Patient instance, from parsed data
        """

        # Use given adapter to parse "row" data
        adapter = adapter_cls(data)

        # Populate instance with available data from adapter / row
        patient = cls()
        for key, value in adapter.items():
            if not value:
                continue
            patient._fields[key] = value

        return patient
