<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
  xmlns="http://www.opengis.net/sld"
  xmlns:ogc="http://www.opengis.net/ogc"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.opengis.net/sld http://schemas.opengis.net/sld/1.0.0/StyledLayerDescriptor.xsd">
  <NamedLayer>
    <Name>agb_biomass</Name>
    <UserStyle>
      <Title>Dynamic ramp with env()</Title>
      <FeatureTypeStyle>
        <Rule>
          <RasterSymbolizer>
            <Opacity>${env('opacity',1.0)}</Opacity>
            <ColorMap type="ramp">
              <ColorMapEntry color="${env('cmin','#000000')}" quantity="${env('min',0)}"    opacity="1.0" label="min"/>
              <ColorMapEntry color="${env('cmed','#000000')}" quantity="${env('med',50)}"   opacity="1.0" label="med"/>
              <ColorMapEntry color="${env('cmax','#000000')}" quantity="${env('max',100)}"  opacity="1.0" label="max"/>
            </ColorMap>
          </RasterSymbolizer>
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
