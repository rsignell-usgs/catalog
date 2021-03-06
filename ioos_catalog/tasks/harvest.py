from bson import ObjectId
from datetime import datetime
from lxml import etree
import itertools
import re

from owslib.sos import SensorObservationService
from owslib.swe.sensor.sml import SensorML
from owslib.util import testXMLAttribute, testXMLValue
from owslib.crs import Crs

from pyoos.parsers.ioos.describe_sensor import IoosDescribeSensor
from paegan.cdm.dataset import CommonDataset, _possiblet, _possiblez, _possiblex, _possibley
from petulantbear.netcdf2ncml import *

from shapely.geometry import mapping, box

import geojson
import json

from ioos_catalog import app, db
from ioos_catalog.tasks.send_email import send_service_down_email

def harvest(service_id):
    with app.app_context():
        service = db.Service.find_one( { '_id' : ObjectId(service_id) } )

        if service.service_type == "DAP":
            return DapHarvest(service).harvest()
        elif service.service_type == "SOS":
            return SosHarvest(service).harvest()
        elif service.service_type == "WMS":
            return WmsHarvest(service).harvest()
        elif service.service_type == "WCS":
            return WcsHarvest(service).harvest()

def unicode_or_none(thing):
    try:
        if thing is None:
            return thing
        else:
            try:
                return unicode(thing)
            except:
                return None
    except:
        return None

class Harvester(object):
    def __init__(self, service):
        self.service = service

class SosHarvest(Harvester):
    def __init__(self, service):
        Harvester.__init__(self, service)

    def harvest(self):
        self.sos = SensorObservationService(self.service.get('url'))
        for offering in self.sos.offerings:
            # TODO: We assume an offering should only have one procedure here
            # which will be the case in sos 2.0, but may not be the case right now
            # on some non IOOS supported servers.
            uid = offering.procedures[0]
            sp_uid = uid.split(":")

            # List storing the stations that have already been processed in this SOS server.
            # This is kept and checked later to avoid servers that have the same stations in many offerings.
            processed = []

            # temnplate:  urn:ioos:type:authority:id
            # sample:     ioos:station:wmo:21414
            if sp_uid[2] == "station":   # Station Offering
                if not uid in processed:
                    self.process_station(uid)
                processed.append(uid)
            elif sp_uid[2] == "network": # Network Offering
                network_ds = IoosDescribeSensor(self.sos.describe_sensor(outputFormat='text/xml;subtype="sensorML/1.0.1/profiles/ioos_sos/1.0"', procedure=uid))
                # Iterate over stations in the network and process them individually
                for proc in network_ds.procedures:
                    if proc is not None and proc.split(":")[2] == "station":
                        if not proc in processed:
                            self.process_station(proc)
                        processed.append(proc)

    def process_station(self, uid):
        """ Makes a DescribeSensor request based on a 'uid' parameter being a station procedure """

        GML_NS   = "http://www.opengis.net/gml"
        XLINK_NS = "http://www.w3.org/1999/xlink"

        with app.app_context():

            metadata_value = etree.fromstring(self.sos.describe_sensor(outputFormat='text/xml;subtype="sensorML/1.0.1/profiles/ioos_sos/1.0"', procedure=uid))
            station_ds     = IoosDescribeSensor(metadata_value)

            unique_id = station_ds.id
            if unique_id is None:
                app.logger.warn("Could not get a 'stationID' from the SensorML identifiers.  Looking for a definition of 'http://mmisw.org/ont/ioos/definition/stationID'")
                return

            dataset = db.Dataset.find_one( { 'uid' : unicode(unique_id) } )
            if dataset is None:
                dataset = db.Dataset()
                dataset.uid = unicode(unique_id)

            # Find service reference in Dataset.services and remove (to replace it)
            tmp = dataset.services[:]
            for d in tmp:
                if d['service_id'] == self.service.get('_id'):
                    dataset.services.remove(d)

            # Parsing messages
            messages = []

            # NAME
            name = unicode_or_none(station_ds.shortName)
            if name is None:
                messages.append(u"Could not get a 'shortName' from the SensorML identifiers.  Looking for a definition of 'http://mmisw.org/ont/ioos/definition/shortName'")

            # DESCRIPTION
            description = unicode_or_none(station_ds.longName)
            if description is None:
                messages.append(u"Could not get a 'longName' from the SensorML identifiers.  Looking for a definition of 'http://mmisw.org/ont/ioos/definition/longName'")

            # PLATFORM TYPE
            asset_type = unicode_or_none(station_ds.platformType)
            if asset_type is None:
                messages.append(u"Could not get a 'platformType' from the SensorML identifiers.  Looking for a definition of 'http://mmisw.org/ont/ioos/definition/platformType'")

            # LOCATION is in GML
            gj = None
            loc = station_ds.location
            if loc is not None and loc.tag == "{%s}Point" % GML_NS:
                pos_element = loc.find("{%s}pos" % GML_NS)
                # strip out points
                positions = map(float, testXMLValue(pos_element).split(" "))
                crs = Crs(testXMLAttribute(pos_element, "srsName"))
                if crs.axisorder == "yx":
                    gj = json.loads(geojson.dumps(geojson.Point([positions[1], positions[0]])))
                else:
                    gj = json.loads(geojson.dumps(geojson.Point([positions[0], positions[1]])))
            else:
                messages.append(u"Found an unrecognized child of the sml:location element and did not attempt to process it: %s" % etree.tostring(loc).strip())

            service = {
                # Reset service
                'name'              : name,
                'description'       : description,
                'service_type'      : self.service.get('service_type'),
                'service_id'        : ObjectId(self.service.get('_id')),
                'data_provider'     : self.service.get('data_provider'),
                'metadata_type'     : u'sensorml',
                'metadata_value'    : unicode(etree.tostring(metadata_value)).strip(),
                'messages'          : map(unicode, messages),
                'keywords'          : map(unicode, sorted(station_ds.keywords)),
                'variables'         : map(unicode, sorted(station_ds.variables)),
                'asset_type'        : asset_type,
                'geojson'           : gj,
                'updated'           : datetime.utcnow()
            }

            dataset.services.append(service)
            dataset.updated = datetime.utcnow()
            dataset.save()
            return "Harvested"

class WmsHarvest(Harvester):
    def __init__(self, service):
        Harvester.__init__(self, service)
    def harvest(self):
        pass

class WcsHarvest(Harvester):
    def __init__(self, service):
        Harvester.__init__(self, service)
    def harvest(self):
        pass

class DapHarvest(Harvester):

    def __init__(self, service):
        Harvester.__init__(self, service)

    def get_standard_variables(self, dataset):
        for d in dataset.variables:
            try:
                yield unicode(dataset.variables[d].getncattr("standard_name"))
            except AttributeError:
                pass

    def harvest(self):
        """
        Identify the type of CF dataset this is:
          * UGRID
          * CGRID
          * RGRID
          * DSG
        """

        METADATA_VAR_NAMES   = [u'crs',
                                u'projection']

        # CF standard names for Axis
        STD_AXIS_NAMES       = [u'latitude',
                                u'longitude',
                                u'time',
                                u'forecast_reference_time',
                                u'forecast_period',
                                u'ocean_sigma',
                                u'ocean_s_coordinate_g1',
                                u'ocean_s_coordinate_g2',
                                u'ocean_s_coordinate',
                                u'ocean_double_sigma',
                                u'ocean_sigma_over_z',
                                u'projection_y_coordinate',
                                u'projection_x_coordinate']

        # Some datasets don't define standard_names on axis variables.  This is used to weed them out based on the
        # actual variable name
        COMMON_AXIS_NAMES    = [u'x',
                                u'y',
                                u'lat',
                                u'latitude',
                                u'lon',
                                u'longitude',
                                u'time',
                                u'time_run',
                                u'time_offset',
                                u'ntimes',
                                u'lat_u',
                                u'lon_u',
                                u'lat_v',
                                u'lon_v  ',
                                u'lat_rho',
                                u'lon_rho',
                                u'lat_psi']

        cd = CommonDataset.open(self.service.get('url'))

        # For DAP, the unique ID is the URL
        unique_id = self.service.get('url')

        with app.app_context():
            dataset = db.Dataset.find_one( { 'uid' : unicode(unique_id) } )
            if dataset is None:
                dataset = db.Dataset()
                dataset.uid = unicode(unique_id)

        # Find service reference in Dataset.services and remove (to replace it)
        tmp = dataset.services[:]
        for d in tmp:
            if d['service_id'] == self.service.get('_id'):
                dataset.services.remove(d)

        # Parsing messages
        messages = []

        # NAME
        name = None
        try:
            name = unicode_or_none(cd.nc.getncattr('title'))
        except AttributeError:
            messages.append(u"Could not get dataset name.  No global attribute named 'title'.")

        # DESCRIPTION
        description = None
        try:
            description = unicode_or_none(cd.nc.getncattr('summary'))
        except AttributeError:
            messages.append(u"Could not get dataset description.  No global attribute named 'summary'.")

        # KEYWORDS
        keywords = []
        try:
            keywords = sorted(map(lambda x: unicode(x.strip()), cd.nc.getncattr('keywords').split(",")))
        except AttributeError:
            messages.append(u"Could not get dataset keywords.  No global attribute named 'keywords' or was not comma seperated list.")

        # VARIABLES
        prefix    = ""
        # Add additonal prefix mappings as they become available.
        try:
            standard_name_vocabulary = unicode(cd.nc.getncattr("standard_name_vocabulary"))

            cf_regex = [re.compile("CF-"), re.compile('http://www.cgd.ucar.edu/cms/eaton/cf-metadata/standard_name.html')]

            for reg in cf_regex:
                if reg.match(standard_name_vocabulary) is not None:
                    prefix = "http://mmisw.org/ont/cf/parameter/"
                    break
        except AttributeError:
            pass

        # Get variables with a standard_name
        std_variables = [cd.get_varname_from_stdname(x)[0] for x in self.get_standard_variables(cd.nc) if x not in STD_AXIS_NAMES and len(cd.nc.variables[cd.get_varname_from_stdname(x)[0]].shape) > 0]

        # Get variables that are not axis variables or metadata variables and are not already in the 'std_variables' variable
        non_std_variables = list(set([x for x in cd.nc.variables if x not in itertools.chain(_possibley, _possiblex, _possiblez, _possiblet, METADATA_VAR_NAMES, COMMON_AXIS_NAMES) and len(cd.nc.variables[x].shape) > 0 and x not in std_variables]))

        """
        var_to_get_geo_from = None
        if len(std_names) > 0:
            var_to_get_geo_from = cd.get_varname_from_stdname(std_names[-1])[0]
            messages.append(u"Variable '%s' with standard name '%s' was used to calculate geometry." % (var_to_get_geo_from, std_names[-1]))
        else:
            # No idea which variable to generate geometry from... try to factor variables with a shape > 1.
            try:
                var_to_get_geo_from = [x for x in variables if len(cd.nc.variables[x].shape) > 1][-1]
            except IndexError:
                messages.append(u"Could not find any non-axis variables to compute geometry from.")
            else:
                messages.append(u"No 'standard_name' attributes were found on non-axis variables.  Variable '%s' was used to calculate geometry." % var_to_get_geo_from)
        """

        # LOCATION (from Paegan)
        # Try POLYGON and fall back to BBOX
        gj = None
        for v in itertools.chain(std_variables, non_std_variables):
            try:
                gj = mapping(cd.getboundingpolygon(var=v))
            except (AttributeError, AssertionError, ValueError):
                try:
                    # Returns a tuple of four coordinates, but box takes in four seperate positional argouments
                    # Asterik magic to expland the tuple into positional arguments
                    gj = mapping(box(*cd.get_bbox(var=v)))
                except (AttributeError, AssertionError, ValueError):
                    pass

            if gj is not None:
                # We computed something, break out of loop.
                messages.append(u"Variable %s was used to calculate geometry." % v)
                break

        if gj is None:
            messages.append(u"The underlying 'Paegan' data access library could not determine a bounding BOX for this dataset.")
            messages.append(u"The underlying 'Paegan' data access library could not determine a bounding POLYGON for this dataset.")
            messages.append(u"Failed to calculate geometry using all of the following variables: %s" % ", ".join(itertools.chain(std_variables, non_std_variables)))

        # TODO: compute bounding box using global attributes


        final_var_names = []
        if prefix == "":
            messages.append(u"Could not find a standard name vocabulary.  No global attribute named 'standard_name_vocabulary'.  Variable list may be incorrect or contain non-measured quantities.")
            final_var_names = non_std_variables + std_variables
        else:
            final_var_names = non_std_variables + list(map(unicode, ["%s%s" % (prefix, cd.nc.variables[x].getncattr("standard_name")) for x in std_variables]))

        service = {
            'name'              : name,
            'description'       : description,
            'service_type'      : self.service.get('service_type'),
            'service_id'        : ObjectId(self.service.get('_id')),
            'data_provider'     : self.service.get('data_provider'),
            'metadata_type'     : u'ncml',
            'metadata_value'    : unicode(dataset2ncml(cd.nc, url=self.service.get('url'))),
            'messages'          : map(unicode, messages),
            'keywords'          : keywords,
            'variables'         : map(unicode, final_var_names),
            'asset_type'        : unicode(cd._datasettype).upper(),
            'geojson'           : gj,
            'updated'           : datetime.utcnow()
        }

        with app.app_context():
            dataset.services.append(service)
            dataset.updated = datetime.utcnow()
            dataset.save()

        return "Harvested"
