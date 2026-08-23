"""Shared Processing-parameter helper.

QgsProcessingParameterNumber's constructor does not accept a 'decimals'
keyword in any QGIS version (confirmed against the QGIS 3.28-3.44 PyQGIS
API): passing one raises ``TypeError: 'decimals' is an unknown keyword
argument``. Spin-box decimal precision is instead set after construction via
QgsProcessingParameterDefinition.setMetadata(), using the
``{'widget_wrapper': {'decimals': N}}`` convention QGIS's own Processing
algorithm dialog reads to configure the numeric spin box. This helper keeps
that two-step dance in one place instead of repeating it at every call site.
"""
from qgis.core import QgsProcessingParameterNumber


def number_param(name, description, default_value, min_value, max_value,
                  decimals=None, optional=False,
                  param_type=QgsProcessingParameterNumber.Double):
    p = QgsProcessingParameterNumber(
        name, description,
        type=param_type,
        defaultValue=default_value,
        optional=optional,
        minValue=min_value,
        maxValue=max_value,
    )
    if decimals is not None:
        p.setMetadata({'widget_wrapper': {'decimals': decimals}})
    return p
