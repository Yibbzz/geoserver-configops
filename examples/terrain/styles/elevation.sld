<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld" xmlns:ogc="http://www.opengis.net/ogc"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.opengis.net/sld http://schemas.opengis.net/sld/1.0.0/StyledLayerDescriptor.xsd">
  <NamedLayer>
    <Name>elevation</Name>
    <UserStyle>
      <Title>Heights in metres, sea green to mountain white</Title>
      <FeatureTypeStyle>
        <Rule>
          <RasterSymbolizer>
            <ColorMap type="ramp">
              <ColorMapEntry color="#2b83ba" quantity="0" opacity="0" label="sea"/>
              <ColorMapEntry color="#4a9e5c" quantity="0.1" label="0 m"/>
              <ColorMapEntry color="#a6d96a" quantity="100"/>
              <ColorMapEntry color="#ffffbf" quantity="250"/>
              <ColorMapEntry color="#d7a96a" quantity="500"/>
              <ColorMapEntry color="#a0715a" quantity="800"/>
              <ColorMapEntry color="#ffffff" quantity="1345" label="1,345 m"/>
            </ColorMap>
          </RasterSymbolizer>
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
