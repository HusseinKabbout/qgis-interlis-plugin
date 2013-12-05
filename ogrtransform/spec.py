import json
import tempfile
from xml.etree import ElementTree
from format_handler import FormatHandlerRegistry
try:
    from osgeo import ogr
    from osgeo import gdal
except ImportError:
    import ogr
    import gdal

# Mapping of OGR integer geometry types to GeoJSON type names. (from Fiona)

GEOMETRY_TYPES = {
    0: 'Unknown',
    1: 'Point',
    2: 'LineString',
    3: 'Polygon',
    4: 'MultiPoint',
    5: 'MultiLineString',
    6: 'MultiPolygon',
    7: 'GeometryCollection',
    100: 'None',
    101: 'LinearRing',
    0x80000001: '3D Point',
    0x80000002: '3D LineString',
    0x80000003: '3D Polygon',
    0x80000004: '3D MultiPoint',
    0x80000005: '3D MultiLineString',
    0x80000006: '3D MultiPolygon',
    0x80000007: '3D GeometryCollection'
}

# Mapping of OGR integer field types to VRT field type names.

FIELD_TYPES = [
    'Integer',      # OFTInteger, Simple 32bit integer
    'IntegerList',  # OFTIntegerList, List of 32bit integers
    'Real',         # OFTReal, Double Precision floating point
    'RealList',     # OFTRealList, List of doubles
    'String',       # OFTString, String of ASCII chars
    'StringList',   # OFTStringList, Array of strings
    None,           # OFTWideString, deprecated
    None,           # OFTWideStringList, deprecated
    'Binary',       # OFTBinary, Raw Binary data
    'Date',         # OFTDate, Date
    'Time',         # OFTTime, Time
    'DateTime'      # OFTDateTime, Date and Time
]


class Spec:

    format_handlers = FormatHandlerRegistry()

    def __init__(self, ds, spec=None, model=None):
        self._ds_fn = ds
        self._ds = None
        self._spec = self._load(spec)
        self._model = model

    def _load(self, fn):
        spec = None
        if fn is not None:
            with open(fn) as file:
                spec = json.load(file)
        return spec

    def open(self):
        self._ds = ogr.Open(self._ds_fn, update=False)
        return self._ds

    def close(self):
        if self._ds is not None:
            self._ds.Destroy()

    def _enums(self, src_format_handler, dst_format_handler):
        enum_tables = {}
        if self._model:
            enums = src_format_handler.extract_enums(self._model)
            for src_name, values in enums.items():
                dst_name = dst_format_handler.shorten_name(src_name, 'enum')
                enum_tables[dst_name] = {
                    'src_name': src_name,
                    'values': values
                }
        return enum_tables

    def generate_spec(self, dst_format, outfile=None, layer_list=[]):
        if self._ds is None:
            self.open()

        if len(layer_list) == 0:
            for layer in self._ds:
                layer_list.append(layer.GetLayerDefn().GetName())

        src_format = self._ds.GetDriver().GetName()
        src_format_handler = Spec.format_handlers.handler(src_format)
        dst_format_handler = Spec.format_handlers.handler(dst_format)

        self._spec = {}

        #Javscript comments are not allowed JSON
        self._spec['//'] = 'OGR transformation specification'
        self._spec['src_format'] = src_format
        self._spec['dst_format'] = dst_format
        layers = {}
        self._spec['layers'] = layers

        for name in layer_list:
            layer = self._ds.GetLayerByName(name)
            layerdef = layer.GetLayerDefn()

            speclayer = {}
            layer_name = dst_format_handler.launder_name(name)
            layers[layer_name] = speclayer
            speclayer['src_layer'] = name
            fields = {}
            speclayer['fields'] = fields

            for fld_index in range(layerdef.GetFieldCount()):
                src_fd = layerdef.GetFieldDefn(fld_index)

                specfield = {}
                field_name = src_fd.GetName()
                dst_name = dst_format_handler.launder_name(field_name)
                fields[dst_name] = specfield
                specfield['src'] = field_name
                jsontype = FIELD_TYPES[src_fd.GetType()]
                specfield['type'] = jsontype
                if src_fd.GetWidth() > 0:
                    specfield['width'] = src_fd.GetWidth()
                if src_fd.GetPrecision() > 0:
                    specfield['precision'] = src_fd.GetPrecision()

            geom_type = GEOMETRY_TYPES[layerdef.GetGeomType()]
            speclayer['geometry_type'] = geom_type

        enum_tables = self._enums(src_format_handler, dst_format_handler)
        if enum_tables:
            self._spec['enums'] = enum_tables

        specstr = json.dumps(self._spec, indent=2)

        if outfile is not None:
            f = open(outfile, "w")
            f.write(specstr)
            f.close()

        return specstr

    def src_format(self):
        return self._spec['src_format']

    def dst_format(self):
        return self._spec['dst_format']

    def generate_vrt(self):
        xml = ElementTree.Element('OGRVRTDataSource')
        for layer_name, speclayer in self._spec['layers'].items():
            layer_node = ElementTree.SubElement(xml, "OGRVRTLayer")
            layer_node.set('name', layer_name)
            node = ElementTree.SubElement(layer_node, "SrcDataSource")
            node.set('relativeToVRT', '0')
            node.set('shared', '1')
            node.text = self._ds_fn
            node = ElementTree.SubElement(layer_node, "SrcLayer")
            node.text = speclayer['src_layer']
            node = ElementTree.SubElement(layer_node, "GeometryType")
            node.text = 'wkb' + speclayer['geometry_type']
            for dst_name, specfield in speclayer['fields'].items():
                node = ElementTree.SubElement(layer_node, "Field")
                node.set('name', dst_name)
                node.set('type', specfield['src'])
                node.set('src', specfield['type'])
                if 'width' in specfield:
                    node.set('width', specfield['width'])
                if 'precision' in specfield:
                    node.set('precision', specfield['precision'])
        return ElementTree.tostring(xml, 'utf-8')

    def vrt_memfile(self):
        vrt_xml = self.generate_vrt()
        self.vrt_memfile = tempfile.mktemp('.vrt', 'ogr_', '/vsimem')
        # Create in-memory file
        gdal.FileFromMemBuffer(self.vrt_memfile, vrt_xml)
        return self.vrt_memfile

    def free_vrt_datasource(self):
        # Free memory associated with the in-memory file
        gdal.Unlink(self.vrt_memfile)

    def vrt_datasource(self):
        #Call free_vrt_datasource after closing datasource to free memeroy
        vrt = self.vrt_memfile()
        ds = ogr.Open(vrt)
        return ds
